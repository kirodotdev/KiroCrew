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

import json
import logging
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import Any

# Computer-use tool names and their argument bounds. Safe to import at module
# scope: ``computer_use.types`` is deliberately dependency-free (it imports
# nothing from ``kiro_crew`` and never touches ctypes), so there is no cycle and
# no native library is loaded on any platform. Aliased so the schema block below
# reads as "the computer-use vocabulary" rather than bare names.
from kiro_crew.computer_use import types as _cu_types
from kiro_crew.constants import (
    AWS_PROFILE_NAME_RE,
    CHANNEL_OWNER_DM_NAMESPACES,
    MAX_BANNER_CHARS,
    SLACK_NAMESPACE,
    WINDOWS_DEVICE_STEMS,
)

# Reasoning-effort vocabulary: ``effort.py`` is the single source of truth for
# the valid levels; EFFORT_VALUES additionally admits ``""`` ("unset — defer to
# the role pin / provider default"). Import-safe: ``effort`` pulls in only
# ``model_registry`` (stdlib-only), so no cycle back into validation.
from kiro_crew.effort import EFFORT_VALUES
from kiro_crew.monitoring.models import (
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_RUNTIME_SECS,
    MAX_MONITOR_STOP_REASON_CHARS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
)
from kiro_crew.project_scope import SCOPE_FRAGMENT_RE

# ── Constants ──

# Max lengths for string inputs
MAX_TOOL_NAME_LEN = 256
MAX_SHORT_STRING = 500  # names, IDs, categories
MAX_MEDIUM_STRING = 5_000  # messages, rules
MAX_LONG_STRING = 50_000  # task specs, inline content
# A cron job's message IS a task prompt (real dispatched task specs routinely
# exceed 5k chars), so it gets its own cap at task-spec scale instead of
# borrowing MAX_MEDIUM_STRING — raising that shared constant would widen ~20
# unrelated fields. Enforced at every create/update surface (MCP schemas,
# dashboard REST) AND at the CronService persistence chokepoint
# (_build_job/update_job), so the CLI and apps SDK cannot admit a larger value
# than the validated surfaces.
MAX_CRON_MESSAGE = 50_000
MAX_RESPONSE_LEN = 100_000  # truncate tool responses

# Allowed categories for lessons
ALLOWED_LESSON_CATEGORIES = frozenset({"tool", "preference", "knowledge"})


def normalize_lesson_category(value: object, *, strict: bool) -> str:
    """Normalize a lesson category to a usable string label.

    The single source of the category rules for every surface that labels a
    lesson, so write-time and display-time policy cannot drift apart:

    - ``strict=True`` (write path): clamp to ``ALLOWED_LESSON_CATEGORIES`` --
      an unrecognized or non-string label is not a category, and "knowledge"
      is the default every other writer uses. The ``isinstance`` check runs
      first so an unhashable label (a dict or list from an LLM) cannot make
      the set membership test raise.
    - ``strict=False`` (display surfaces): only default non-string or blank
      values, passing any other non-blank string through -- a category
      accepted at write time keeps its own label when rendered.
    """
    if not isinstance(value, str):
        return "knowledge"
    if strict:
        return value if value in ALLOWED_LESSON_CATEGORIES else "knowledge"
    return value if value.strip() else "knowledge"


# Allowed scopes for lessons (mirrors the learn_add MCP inputSchema enum).
ALLOWED_LESSON_SCOPES = frozenset({"global", "workspace"})

# Allowed cron schedule kinds
ALLOWED_SCHEDULE_KINDS = frozenset({"every", "cron", "at"})

# Allowed hook events for the hook create/update API.
#
# Three event allowlists exist and they diverge INTENTIONALLY; a follow-up must
# not "fix" the drift by syncing them. test_session_lane_changed_hook.py pins all
# three memberships together with this rationale:
#   * hooks.HOOK_EVENTS        -- what the dispatcher can fire. Carries every event.
#   * this set                 -- what the registration API accepts. A
#                                 dashboard-internal event still has to be here,
#                                 or hand-editing hooks.json is the only way to
#                                 register a hook for it.
#   * agent._VALID_HOOK_EVENTS -- what a generated kiro-cli agent config may
#                                 contain. SessionLaneChanged must stay OUT:
#                                 kiro-cli rejects a spec naming an event it does
#                                 not know, so adding it there breaks the config.
ALLOWED_HOOK_EVENTS = frozenset(
    {"AgentSpawn", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionLaneChanged"}
)

# Valid agent name pattern (alphanumeric, hyphens, underscores)
_AGENT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")

# Artifact slug grammar — mirrors kiro_crew.artifacts._SLUG_RE (kept here so
# consumers outside the store module share one public definition). Used to
# validate the companion-chat `artifact` slot binding at EVERY
# boundary it crosses: slot create (chat_handlers) and history-metadata
# restore (chat_persistence) — a tampered history JSONL must not be able to
# inject an arbitrary string that flows into to_dict()/WS broadcasts.
# \Z (not $): Python's $ also matches just before a trailing newline, so
# "valid-slug\n" would pass a $-anchored .match() — \Z anchors at the true
# end of the string.
ARTIFACT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Valid model name pattern — alphanumerics, hyphens, dots (e.g. "claude-opus-4.8", "deepseek-3.2")
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Content-bound theme-persona consent hash: sha256 rendered as EXACTLY 64
# lowercase hex chars. This value flows into hmac.compare_digest at the
# persona-injection site (chat_utils._maybe_inject_persona); compare_digest
# raises TypeError on any non-ASCII str (e.g. "é"), which would abort the whole
# chat turn. Validating full-match here means anything malformed is
# treated as ABSENT (fail closed: no injection, no crash). \Z (not $) anchors
# the true end of the string so a trailing newline can't sneak through.
THEME_CONSENT_SHA_RE = re.compile(r"[0-9a-f]{64}\Z")


def normalize_theme_consent_sha(value: Any) -> str | None:
    """Return a canonical 64-lowercase-hex consent sha, or ``None`` if absent
    or malformed.

    Fail-closed normalizer for the ``theme_consent_sha`` request field. A
    non-str, or any string that is not exactly 64 hex chars after ``strip()`` +
    ``lower()``, yields ``None`` (treated as no consent). This guarantees the
    value ever handed to ``hmac.compare_digest`` is pure ASCII hex, so a
    non-ASCII or otherwise malformed value can never crash the turn.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if THEME_CONSENT_SHA_RE.fullmatch(candidate):
        return candidate
    return None


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
# threads key their session off the canonical namespaced form
# ``slack:<thread_ts>`` (see messaging/link.py and slack/handler.py), but the
# legacy bare thread_ts form persists in older session maps, conversation logs,
# and callers — distinct from the "slack:<chan>:<ts>" delivery-target form —
# so callers that authorize by session key must accept both the bare shape and
# the ``slack:`` prefix (see ``is_channel_ns`` in ``api_lessons_create``).
#
# Use the explicit ASCII class ``[0-9]`` (not ``\d``): in Python 3 ``\d`` also
# matches non-ASCII Unicode decimal digits (Arabic-Indic ٠-٩, Devanagari ०-९,
# etc.). Because this pattern gates an authorization decision (``is_channel_ns``
# in ``api_lessons_create``), ``\d`` would let a crafted key built from Unicode
# digits pass as a Slack thread_ts, matching the ASCII-only intent of the other
# patterns in this file (e.g. ``CHANNEL_ID_RE``).
SLACK_THREAD_TS_RE = re.compile(r"^[0-9]{10,}\.[0-9]{6,}$")


def infer_use_case(session_key: str) -> str:
    """Map a KiroCrew session_key to a categorical useCase label.

    Returns ``"unknown"`` for unrecognized shapes — never raises. Pure string
    matching on the session key; lives here next to ``SLACK_THREAD_TS_RE`` so
    authorization (learn_add) and classification stay in lockstep.

    Limitations: ``cli_chat`` and ``_bg`` collapse multiple
    invocations into one session each.
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
# Categories removed wholesale. ``Cf`` (format) is deliberately NOT here: it
# holds ZWJ U+200D, ZWNJ U+200C and the variation selectors that emoji
# sequences and Arabic / Persian / Indic scripts REQUIRE to render correctly,
# so deleting the category corrupts user content instead of hardening anything
# (``dashboard/chat_folders.py`` already treats U+200D / U+FE0F as meaningful
# emoji modifiers, and test_context_marker_neutralization asserts ZWNJ/ZWJ
# survive). ``Co`` (private use) is likewise excluded — Nerd Fonts and terminal
# themes carry real icon glyphs there. The genuinely dangerous Cf members are
# removed by codepoint via ``_BIDI_CONTROLS`` instead of by category.
_HIDDEN_CATEGORIES = frozenset(
    {
        "Cc",  # control (except \n \r \t)
        "Cs",  # surrogate — never valid in well-formed text
    }
)

# Categories removed wholesale. ``Cf`` (format) IS included — it is stripped by
# default and only the shaping characters named in ``_ALLOWED_FORMAT`` below get
# through. Fail-closed is required here rather than aesthetic: this sanitizer
# runs BEFORE credential redaction, so any invisible character it preserves can
# be inserted into a credential to defeat ``redact_credentials``' patterns and
# carry a recoverable secret into the dashboard and the notification JSONL. An
# allowlist means a newly-assigned or simply un-enumerated ``Cf`` codepoint is
# blocked instead of silently becoming an evasion vector.
#
# ``Co`` (private use) is excluded: Nerd Fonts and terminal themes carry real
# icon glyphs there, and unlike ``Cf`` those are visible, so they cannot hide a
# credential from a human reader.
_HIDDEN_CATEGORIES = frozenset(
    {
        "Cc",  # control (except \n \r \t)
        "Cf",  # format — see _ALLOWED_FORMAT for the narrow exceptions
        "Cs",  # surrogate — never valid in well-formed text
    }
)

# The ONLY format characters allowed through. Each has a real text-shaping job
# that scripts and emoji sequences cannot express without it, so removing them
# corrupts user content (``dashboard/chat_folders.py`` treats U+200D as a
# meaningful emoji modifier, and test_context_marker_neutralization asserts
# ZWNJ/ZWJ survive).
#
# Everything else in ``Cf`` stays denied, including ZWSP U+200B, the word joiner
# and invisible operators U+2060-2064, BOM U+FEFF, the bidi embedding/override/
# isolate controls (Trojan Source, CVE-2021-42574), interlinear annotation
# controls, and SOFT HYPHEN U+00AD.
#
# NOTE: the variation selectors (U+FE00-FE0F) are category ``Mn``, not ``Cf``,
# so they were never subject to this rule and need no entry here.
_ALLOWED_FORMAT = frozenset(
    {
        "\u200c",  # ZWNJ — required by Persian / Arabic / Indic orthography
        "\u200d",  # ZWJ — welds emoji sequences; required by Indic scripts
        "\u200e",  # LRM — ordinary mark in mixed-direction text
        "\u200f",  # RLM — ordinary mark in mixed-direction text
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


# ── MCP inputSchema validation (JSON Schema subset, fail-closed) ──

#: Hard caps so an adversarial payload/schema cannot DoS the validator.
_MCP_ARGS_MAX_DEPTH = 16
_MCP_ARGS_MAX_STRING = 1_000_000  # 1MB per string leaf

#: Bounds for evaluating a schema ``pattern`` against an untrusted value. The
#: PATTERN is server-controlled and the VALUE is app-controlled — a
#: pathological pair (e.g. ``(a+)+$`` + a long near-match) is a classic ReDoS.
_PATTERN_MAX_LEN = 512
_PATTERN_VALUE_MAX_LEN = 4096
_PATTERN_TIMEOUT_SECS = 2.0

#: Child source for the sandboxed match. CPython's ``re`` engine holds the GIL
#: for the entire match, so an in-process timeout (thread + join) cannot stop a
#: catastrophic backtrack — the abandoned worker would starve the whole
#: process. A subprocess is the only stdlib mechanism that both bounds the
#: wall clock AND kills the runaway CPU (``subprocess.run`` kills the child on
#: timeout). Exit codes: 0 = matched, 1 = no match, 3 = invalid pattern.
_PATTERN_CHILD_SRC = (
    "import json,re,sys\n"
    "d=json.load(sys.stdin)\n"
    "try:\n"
    "    sys.exit(0 if re.search(d['p'], d['v']) else 1)\n"
    "except re.error:\n"
    "    sys.exit(3)\n"
)


def _bounded_pattern_search(pattern: str, value: str) -> bool | None:
    """``re.search`` with hard input-size caps and a killable wall-clock bound.

    Returns True/False for a completed match check, or ``None`` when the
    check could not be performed safely (oversized pattern/value, invalid
    pattern, or timeout) — callers treat ``None`` as fail-closed. The match
    runs in a short-lived subprocess that is KILLED on timeout, so a
    server-supplied ReDoS pattern cannot burn CPU beyond the bound (an
    in-process thread cannot be stopped and would hold the GIL for the whole
    catastrophic match). Pattern checks happen at app-interaction rate, so
    the interpreter-startup cost is acceptable.
    """
    if len(pattern) > _PATTERN_MAX_LEN or len(value) > _PATTERN_VALUE_MAX_LEN:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, own interpreter
            [sys.executable, "-I", "-c", _PATTERN_CHILD_SRC],
            input=json.dumps({"p": pattern, "v": value}).encode("utf-8"),
            capture_output=True,
            timeout=_PATTERN_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return None  # runaway match killed — fail closed
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # invalid pattern (or unexpected child failure) — fail closed


_JSON_SCHEMA_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}


def _json_type_ok(value: Any, type_name: str) -> bool:
    expected = _JSON_SCHEMA_TYPES.get(type_name)
    if expected is None:
        # Unknown type name in the schema — fail closed rather than admit.
        return False
    if not isinstance(value, expected):
        return False
    # bool is a subclass of int: keep number/integer honest.
    if isinstance(value, bool) and type_name in ("number", "integer"):
        return False
    return True


# Validation keywords the subset ENFORCES. A schema using any validation
# keyword outside this set (oneOf, allOf, $ref, patternProperties, if/then,
# multipleOf, contains, …) is REJECTED fail-closed rather than partially
# enforced — ignoring a constraint the tool author wrote would forward values
# the tool declared forbidden.
_SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "pattern",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    }
)

# Annotation-only keywords (non-validating per JSON Schema draft 2020-12 —
# ``format`` is annotation-only unless the format-assertion vocabulary is
# explicitly enabled, which MCP does not require). Safe to ignore.
_ANNOTATION_SCHEMA_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "examples",
        "format",
        "$schema",
        "$id",
        "$comment",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)


def _reject_unsupported_keywords(schema: dict, path: str) -> None:
    unsupported = [
        k
        for k in schema
        if k not in _SUPPORTED_SCHEMA_KEYWORDS and k not in _ANNOTATION_SCHEMA_KEYWORDS
    ]
    if unsupported:
        raise ValidationError(
            path,
            "schema uses unsupported validation keyword(s) "
            f"{sorted(unsupported)}; failing closed",
        )


def _json_equal(a: Any, b: Any) -> bool:
    """JSON-semantics equality for ``enum``/``const``/``uniqueItems``.

    Python conflates ``True == 1`` and ``False == 0``, so a schema
    ``{"enum": [1]}`` or ``{"const": 1}`` would wrongly admit boolean ``true``
    (and vice-versa), forwarding a value the declared contract forbids. JSON
    keeps booleans and numbers as distinct types, so a boolean may equal ONLY a
    boolean. Numeric ``1 == 1.0`` is preserved (JSON has one number type).
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    return a == b


def _reject_malformed_keyword_shapes(schema: dict[str, Any], _path: str) -> None:
    """Reject a keyword present with the WRONG value shape (e.g. ``"type": 7``).

    Fail-closed: a malformed constraint must never be silently skipped and
    treated as "no constraint". Only shape is checked here — semantics are
    enforced by the main validator.
    """

    def _bad(key: str) -> None:
        raise ValidationError(_path, f"malformed `{key}` keyword shape")

    t = schema.get("type")
    if "type" in schema and not (
        isinstance(t, str) or (isinstance(t, list) and all(isinstance(x, str) for x in t))
    ):
        _bad("type")
    if "enum" in schema and not isinstance(schema["enum"], list):
        _bad("enum")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(x, str) for x in schema["required"])
    ):
        _bad("required")
    if "properties" in schema and not isinstance(schema["properties"], dict):
        _bad("properties")
    if isinstance(schema.get("properties"), dict):
        # Every property VALUE must be a subschema (object) or a boolean
        # subschema. A malformed value (e.g. the string "false") would otherwise
        # recurse to the "no subschema → return" branch and admit the field
        # UNVALIDATED — a fail-open hole. Reject the schema instead.
        for _pk, _pv in schema["properties"].items():
            if not isinstance(_pv, (dict, bool)):
                _bad("properties")
    if "additionalProperties" in schema and not isinstance(
        schema["additionalProperties"], (dict, bool)
    ):
        # Same fail-open risk for a malformed additionalProperties subschema.
        _bad("additionalProperties")
    if "items" in schema and not isinstance(schema["items"], (dict, bool)):
        _bad("items")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        _bad("uniqueItems")
    if "pattern" in schema and not isinstance(schema["pattern"], str):
        _bad("pattern")
    for k in ("minLength", "maxLength", "minItems", "maxItems"):
        if k in schema and (not isinstance(schema[k], int) or isinstance(schema[k], bool)):
            _bad(k)
    for k in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if k in schema and (not isinstance(schema[k], (int, float)) or isinstance(schema[k], bool)):
            _bad(k)


def validate_mcp_tool_arguments(
    arguments: Any, input_schema: Any, *, _path: str = "arguments", _depth: int = 0
) -> None:
    """Validate untrusted tool ``arguments`` against a declared MCP
    ``inputSchema`` (a JSON Schema subset). Raises :class:`ValidationError`.

    This is the shared boundary check for tool inputs that arrive from
    UNTRUSTED callers (e.g. an embedded MCP App iframe relaying
    ``tools/call``) rather than from the model. Fail-closed semantics —
    deliberately stricter than vanilla JSON Schema:

    * A tool that declares no usable ``inputSchema`` accepts ONLY ``{}``.
    * When ``properties`` is declared, unknown keys are rejected unless the
      schema explicitly sets ``additionalProperties`` to ``true`` or a schema.
    * Unknown ``type`` names in the schema reject rather than admit.
    * A schema using a validation keyword this subset does not enforce
      (``oneOf``, ``$ref``, ``multipleOf``, …) REJECTS the call outright —
      constraints are never silently dropped. Annotation-only keywords
      (``title``, ``description``, ``format``, …) are ignored.

    Enforced keywords: ``type`` (str or list), ``required``, ``properties``,
    ``additionalProperties``, ``items``, ``enum``, ``const``, ``pattern``,
    ``minimum``/``maximum`` (+ exclusive forms), ``minLength``/``maxLength``,
    ``minItems``/``maxItems``, ``uniqueItems``. Transport-level caps bound
    depth and string size regardless of schema.
    """
    if _depth > _MCP_ARGS_MAX_DEPTH:
        raise ValidationError(_path, "exceeds max nesting depth")
    if isinstance(arguments, str) and len(arguments) > _MCP_ARGS_MAX_STRING:
        raise ValidationError(_path, "string exceeds max length")

    # JSON Schema boolean subschemas: `true` accepts anything, `false` accepts
    # nothing. Handle explicitly — otherwise a `false` subschema (e.g.
    # `properties.x: false`, or `items: false`) falls through the dict gate and
    # is silently treated as "no constraint", forwarding a forbidden value.
    if input_schema is True:
        return
    if input_schema is False:
        raise ValidationError(_path, "value forbidden by `false` schema")

    if _depth == 0:
        if not isinstance(arguments, dict):
            raise ValidationError(_path, "must be a JSON object")
        if not isinstance(input_schema, dict):
            # No declared contract → only the empty call is admissible.
            if arguments:
                raise ValidationError(
                    _path, "tool declares no inputSchema; only empty arguments allowed"
                )
            return

    schema = input_schema if isinstance(input_schema, dict) else None
    if schema is None:
        return  # nested position with no subschema — nothing more to check
    _reject_unsupported_keywords(schema, _path)
    _reject_malformed_keyword_shapes(schema, _path)

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        if not _json_type_ok(arguments, declared_type):
            raise ValidationError(
                _path, f"expected {declared_type}, got {type(arguments).__name__}"
            )
    elif isinstance(declared_type, list):
        if not any(isinstance(t, str) and _json_type_ok(arguments, t) for t in declared_type):
            raise ValidationError(
                _path,
                f"expected one of {declared_type}, got {type(arguments).__name__}",
            )

    enum = schema.get("enum")
    if isinstance(enum, list) and not any(_json_equal(arguments, e) for e in enum):
        raise ValidationError(_path, "not in enum")
    if "const" in schema and not _json_equal(arguments, schema["const"]):
        raise ValidationError(_path, "does not equal const")

    if isinstance(arguments, str):
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if isinstance(min_len, int) and len(arguments) < min_len:
            raise ValidationError(_path, f"shorter than minLength {min_len}")
        if isinstance(max_len, int) and len(arguments) > max_len:
            raise ValidationError(_path, f"exceeds maxLength {max_len}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            verdict = _bounded_pattern_search(pattern, arguments)
            if verdict is None:
                # Oversized input, invalid pattern, or wall-clock timeout
                # (possible ReDoS) — fail closed rather than forward a value
                # whose declared constraint could not be safely checked.
                raise ValidationError(_path, "pattern constraint could not be safely evaluated")
            if not verdict:
                raise ValidationError(_path, "does not match pattern")

    if isinstance(arguments, (int, float)) and not isinstance(arguments, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and arguments < minimum:
            raise ValidationError(_path, f"must be >= {minimum}")
        if isinstance(maximum, (int, float)) and arguments > maximum:
            raise ValidationError(_path, f"must be <= {maximum}")
        exc_min = schema.get("exclusiveMinimum")
        exc_max = schema.get("exclusiveMaximum")
        if isinstance(exc_min, (int, float)) and arguments <= exc_min:
            raise ValidationError(_path, f"must be > {exc_min}")
        if isinstance(exc_max, (int, float)) and arguments >= exc_max:
            raise ValidationError(_path, f"must be < {exc_max}")

    if isinstance(arguments, list):
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and len(arguments) > max_items:
            raise ValidationError(_path, f"exceeds maxItems {max_items}")
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(arguments) < min_items:
            raise ValidationError(_path, f"fewer than minItems {min_items}")
        if schema.get("uniqueItems") is True:
            # O(n) via canonical JSON keys instead of O(n^2) ``in`` scans (an
            # app-controlled array could otherwise saturate a validation
            # worker). ``json.dumps`` renders ``true``/``1`` distinctly, so this
            # ALSO preserves JSON boolean-vs-number semantics (``True != 1``).
            seen_keys: set[str] = set()
            for item in arguments:
                try:
                    key = json.dumps(item, sort_keys=True, separators=(",", ":"))
                except (TypeError, ValueError):
                    key = repr(item)
                if key in seen_keys:
                    raise ValidationError(_path, "items not unique")
                seen_keys.add(key)
        items = schema.get("items")
        if isinstance(items, (dict, bool)):
            # dict subschema OR a boolean schema (`items: false` rejects every
            # element; `items: true` accepts). Recursion handles all three.
            for i, item in enumerate(arguments):
                validate_mcp_tool_arguments(item, items, _path=f"{_path}[{i}]", _depth=_depth + 1)
        else:
            # No item schema: still bound depth/size of each element.
            for i, item in enumerate(arguments):
                validate_mcp_tool_arguments(item, {}, _path=f"{_path}[{i}]", _depth=_depth + 1)

    if isinstance(arguments, dict):
        properties = schema.get("properties")
        props = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in arguments:
                    raise ValidationError(f"{_path}.{key}", "required")
        additional = schema.get("additionalProperties")
        allow_extra = additional is True or isinstance(additional, dict)
        for key, value in arguments.items():
            if not isinstance(key, str):
                raise ValidationError(_path, "object keys must be strings")
            sub = props.get(key)
            if sub is not None:
                validate_mcp_tool_arguments(value, sub, _path=f"{_path}.{key}", _depth=_depth + 1)
            elif additional is False or (isinstance(properties, dict) and not allow_extra):
                # Fail-closed: an explicit ``additionalProperties: false``
                # rejects every undeclared key even when the schema declares
                # NO ``properties`` map at all (i.e. it accepts only ``{}``),
                # and schemas that enumerate properties reject unlisted keys
                # unless additionalProperties opts in.
                raise ValidationError(f"{_path}.{key}", "unknown field")
            elif isinstance(additional, dict):
                validate_mcp_tool_arguments(
                    value, additional, _path=f"{_path}.{key}", _depth=_depth + 1
                )
            else:
                # No properties map at all: bound depth/size of the blob.
                validate_mcp_tool_arguments(value, {}, _path=f"{_path}.{key}", _depth=_depth + 1)


# ── String Sanitization ──


def strip_hidden_unicode(text: str) -> str:
    """Remove hidden Unicode, preserving only script-essential shaping marks.

    Removes control characters (except \\n, \\r, \\t), surrogates, and category
    ``Cf`` — which covers ZWSP, BOM, the word joiner and invisible operators,
    and the bidi override/isolate controls behind Trojan Source
    (CVE-2021-42574).

    The four marks in :data:`_ALLOWED_FORMAT` are kept ONLY when at least one
    immediate neighbour is non-ASCII. They exist to shape non-ASCII text —
    emoji sequences, Arabic / Persian / Indic orthography — so between two
    ASCII characters they have no rendering effect and their only practical use
    is hiding a credential from redaction: this function runs BEFORE
    ``redact_credentials``, so a surviving invisible inside
    ``AKIA<ZWJ>IOSFODNN7EXAMPLE`` would defeat its patterns and carry a
    recoverable secret to the dashboard and the notification JSONL.

    Tradeoff, accepted deliberately: an LRM/RLM placed between two ASCII
    characters inside otherwise-bidi text is also dropped. That usage is rare
    in tool output, and a per-string "contains any RTL" test would let an
    attacker re-enable the mark by planting one non-ASCII character elsewhere
    in the same response — so the neighbour test is the one that actually
    closes the bypass.
    """
    # Pass 1: drop every hidden character EXCEPT the shaping marks, so the
    # neighbour test below only ever sees characters that actually SURVIVE.
    # Testing against the raw input let a stripped character vouch for a mark
    # (``AKIA<ZWSP><ZWJ>IOSF…``: the ZWSP is removed, but the ZWJ saw it as a
    # non-ASCII neighbour and stayed, leaving the credential unredactable).
    kept: list[str] = []
    for ch in text:
        if (
            ch in _ALLOWED_CONTROL
            or ch in _ALLOWED_FORMAT
            or unicodedata.category(ch) not in _HIDDEN_CATEGORIES
        ):
            kept.append(ch)
    # Pass 2: a shaping mark survives only if the nearest surviving
    # NON-MARK character on one side is non-ASCII. Skipping over adjacent
    # marks is what stops a run of them from vouching for each other —
    # ``"\u200d".isascii()`` is False, so ``AKIA<ZWJ><ZWJ>IOSF…`` would
    # otherwise keep both and stay unredactable.
    total = len(kept)
    out: list[str] = []
    for i, ch in enumerate(kept):
        if ch not in _ALLOWED_FORMAT:
            out.append(ch)
            continue
        left = i - 1
        while left >= 0 and kept[left] in _ALLOWED_FORMAT:
            left -= 1
        right = i + 1
        while right < total and kept[right] in _ALLOWED_FORMAT:
            right += 1
        prev_ch = kept[left] if left >= 0 else ""
        next_ch = kept[right] if right < total else ""
        if (prev_ch and not prev_ch.isascii()) or (next_ch and not next_ch.isascii()):
            out.append(ch)
    return "".join(out)


def normalize_unicode(text: str) -> str:
    """NFC-normalize Unicode text to canonical form."""
    return unicodedata.normalize("NFC", text)


def sanitize_string(text: str) -> str:
    """Full sanitization pipeline: normalize → strip hidden chars → strip edges."""
    text = normalize_unicode(text)
    text = strip_hidden_unicode(text)
    return text.strip()


def sanitize_json_values(value: Any) -> Any:
    """Recursively sanitize every string (keys included) in decoded JSON.

    Schema-level sanitization sees a JSON document only as one opaque string,
    where an escape like ``\\u200b`` is plain ASCII and passes untouched — the
    hidden character only materializes when ``json.loads`` decodes it, AFTER
    the sanitizer ran. Any handler that decodes caller-supplied JSON must walk
    the decoded structure through this before acting on it, or an invisible
    character smuggled inside a credential defeats downstream redaction.
    """
    if isinstance(value, str):
        return sanitize_string(value)
    if isinstance(value, dict):
        return {sanitize_json_values(k): sanitize_json_values(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_json_values(item) for item in value]
    return value


# ── Response Sanitization ──


def sanitize_response(text: str, max_len: int = MAX_RESPONSE_LEN) -> str:
    """Sanitize and truncate a tool response before returning to caller."""
    text = sanitize_string(text)
    if len(text) > max_len:
        # Truncation drops the TAIL, and tail-anchored payloads live there —
        # a session directive's marker is the last line, so a response over the
        # cap loses its effect entirely. Never silent.
        logging.getLogger(__name__).warning(
            "tool response truncated: %d chars over the %d cap — a "
            "tail-anchored marker (e.g. a session directive) would be lost",
            len(text) - max_len,
            max_len,
        )
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
        # Optional per-call reasoning-effort override for the subagent(s).
        # Batch-wide, like ``model``. ``""`` (in EFFORT_VALUES) means "unset —
        # defer to the role_efforts['subagent'] pin, else the provider default".
        FieldSpec("reasoning_effort", str, allowed=EFFORT_VALUES),
        # keep=True makes the run a continuable conversation: its session
        # persists (hibernated on disk) after completion, and spawn_continue
        # can dispatch follow-up turns into it with full prior context.
        FieldSpec("keep", bool),
        # Switchable context groups the sub-agent inherits. Explicit
        # ``default=True`` rather than the implicit ``None``: the semantic
        # default is "on", and without it an explicit JSON ``null`` cleans to
        # ``None``, which a consumer coercing with ``bool()`` would read as a
        # withheld group — the opposite of what the caller asked for.
        FieldSpec("include_memory", bool, default=True),
        FieldSpec("include_lessons", bool, default=True),
        FieldSpec("include_project", bool, default=True),
    ],
)

SPAWN_CONTINUE_SCHEMA = ToolSchema(
    tool_name="spawn_continue",
    fields=[
        FieldSpec("conversation", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("task", str, required=True, max_len=MAX_MEDIUM_STRING),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
        FieldSpec("max_turns", int, min_val=0, max_val=200),
        FieldSpec("model", str, max_len=MAX_SHORT_STRING, pattern=_MODEL_NAME_RE),
    ],
)

SPAWN_STEER_SCHEMA = ToolSchema(
    tool_name="spawn_steer",
    fields=[
        FieldSpec("agent_id", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("message", str, required=True, max_len=MAX_MEDIUM_STRING),
        FieldSpec("mode", str, pattern=re.compile(r"^(interrupt|follow_up)$")),
    ],
)

SPAWN_RELEASE_SCHEMA = ToolSchema(
    tool_name="spawn_release",
    fields=[
        FieldSpec("conversation", str, required=True, max_len=MAX_SHORT_STRING),
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
        # Context groups, as on spawn_run: batch-wide, all default True.
        FieldSpec("include_memory", bool, default=True),
        FieldSpec("include_lessons", bool, default=True),
        FieldSpec("include_project", bool, default=True),
    ],
)

LEARN_ADD_SCHEMA = ToolSchema(
    tool_name="learn_add",
    fields=[
        FieldSpec("rule", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("category", str, allowed=ALLOWED_LESSON_CATEGORIES, default="knowledge"),
        FieldSpec("negative", str, max_len=MAX_SHORT_STRING),
        # Path fragment naming the repository a correction belongs to; absent means
        # it applies everywhere. The pattern is the gate's own (imported, not
        # restated), so a value the gate could never satisfy -- a dot segment,
        # traversal, an empty segment, a drive-qualified path -- is refused HERE
        # rather than stored as a lesson that reports success and applies nowhere.
        # Whether the named path exists is still the gate's business, at injection.
        FieldSpec("repo_scope", str, max_len=MAX_SHORT_STRING, pattern=SCOPE_FRAGMENT_RE),
        # scope/workspace: the /api/lessons handler stores and lists
        # workspace-scoped lessons, but that tier does NOT reach a prompt -- the
        # context builder gates injected lessons on repo_scope instead. The
        # "workspace required when scope='workspace'" rule is enforced in the
        # handler.
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

# Session work ledger (session_ledger.py). Field caps mirror the core module's
# own clamps so the route refuses loudly what the primitive would otherwise
# truncate silently. ``artifacts`` inner shape (str->str, bounded) is enforced
# by the core module's merge. No slot-key field on purpose: the backend
# resolves the calling session's identity from the request, never the body.
SESSION_LEDGER_RECORD_SCHEMA = ToolSchema(
    tool_name="session_ledger_record",
    fields=[
        FieldSpec("goal", str, max_len=2000),
        FieldSpec("phase", str, max_len=128),
        FieldSpec("next", str, max_len=2000),
        FieldSpec("tried_approach", str, max_len=2000),
        FieldSpec("tried_rejected_because", str, max_len=2000),
        FieldSpec("artifacts", dict),
        FieldSpec("event", str, max_len=2000),
        FieldSpec("event_kind", str, max_len=32),
    ],
)
# Empty on purpose, and REGISTERED on purpose: with no schema in
# MCP_CORE_SCHEMAS an unexpected argument passes through unvalidated
# (the learn_list gap), while an empty registered schema rejects it
# (the spawn_list / resource_status precedent). The tool takes no
# arguments; the schema's job is to enforce exactly that.
SESSION_LEDGER_READ_SCHEMA = ToolSchema(tool_name="session_ledger_read")

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
RESOURCE_STATUS_SCHEMA = ToolSchema(tool_name="resource_status")

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

MONITOR_WATCH_SCHEMA = ToolSchema(
    tool_name="monitor_watch",
    fields=[
        FieldSpec("kind", str, required=True, allowed=frozenset({"github_pull_request"})),
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("objective", str, required=True, allowed=frozenset({"review_ready"})),
        FieldSpec(
            "interval_secs",
            int,
            min_val=MIN_MONITOR_CADENCE_SECS,
            max_val=MAX_MONITOR_CADENCE_SECS,
        ),
        FieldSpec("max_runtime_secs", int, min_val=1, max_val=MAX_MONITOR_RUNTIME_SECS),
        FieldSpec("max_agent_turns", int, min_val=1, max_val=MAX_MONITOR_AGENT_TURNS),
        FieldSpec("max_tokens", int, min_val=1, max_val=MAX_MONITOR_TOKENS),
        FieldSpec("max_provider_errors", int, min_val=1, max_val=MAX_MONITOR_PROVIDER_ERRORS),
        FieldSpec("wake_instructions", str, max_len=MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS),
    ],
)

MONITOR_INSPECT_SCHEMA = ToolSchema(tool_name="monitor_inspect")
MONITOR_STOP_SCHEMA = ToolSchema(
    tool_name="monitor_stop",
    fields=[FieldSpec("reason", str, max_len=MAX_MONITOR_STOP_REASON_CHARS)],
)

# monitor_start creates an AutoNudge loop bound to the calling session (the
# agent-facing "babysit this PR" primitive). message caps match the REST
# endpoint's 8000-char limit; interval bounds mirror autonudge's
# _MIN_IDLE_SECS/_MAX_IDLE_SECS clamp. max_runtime_secs is the wall-clock
# budget (0 = unlimited); the 7-day ceiling keeps a typo like 6e9 from arming
# an effectively-unbounded loop while still covering week-long babysits.
MONITOR_START_SCHEMA = ToolSchema(
    tool_name="monitor_start",
    fields=[
        FieldSpec("message", str, required=True, max_len=8000),
        FieldSpec("interval_secs", int, min_val=15, max_val=86400),
        FieldSpec("max_cycles", int, min_val=0, max_val=1000),
        FieldSpec("max_runtime_secs", int, min_val=0, max_val=604800),
        # Opt-OUT of observation gating. Absent means gated, matching the tool's
        # default, so a caller written before this field existed keeps the
        # default behaviour rather than silently escaping it.
        FieldSpec("gate", bool),
        # The short row shown in the transcript instead of the full message. An
        # ENTRY bound only: the authorised add path re-checks the cap AFTER
        # redaction, which is the one that governs what gets stored, because
        # redaction can grow the string.
        FieldSpec("banner", str, max_len=MAX_BANNER_CHARS),
    ],
)

# monitor_update revises the loop already bound to the calling session. Every
# field is optional (a no-field call is a no-op the handler rejects), and the
# bounds mirror MONITOR_START_SCHEMA so a loop cannot be updated into a state
# that monitor_start would have refused to create.
MONITOR_UPDATE_SCHEMA = ToolSchema(
    tool_name="monitor_update",
    fields=[
        FieldSpec("message", str, max_len=8000),
        FieldSpec("interval_secs", int, min_val=15, max_val=86400),
        FieldSpec("max_cycles", int, min_val=0, max_val=1000),
        FieldSpec("max_runtime_secs", int, min_val=0, max_val=604800),
        FieldSpec("target", str, max_len=MAX_SHORT_STRING),
        FieldSpec("objective", str, allowed=frozenset({"review_ready"})),
        FieldSpec("max_agent_turns", int, min_val=1, max_val=MAX_MONITOR_AGENT_TURNS),
        FieldSpec("max_tokens", int, min_val=1, max_val=MAX_MONITOR_TOKENS),
        FieldSpec("max_provider_errors", int, min_val=1, max_val=MAX_MONITOR_PROVIDER_ERRORS),
        FieldSpec("wake_instructions", str, max_len=MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS),
        # Same bound as the arm side, for the reason the comment above gives: a
        # loop must not be updatable into a state monitor_start would refuse.
        FieldSpec("banner", str, max_len=MAX_BANNER_CHARS),
    ],
)

# Bounds for the question-card payload, shared by ASK_QUESTION_SCHEMA (agent-facing
# arg check) and validate_ask_user_question (authoritative payload normalization).
_ASK_MAX_QUESTIONS = 4
_ASK_MAX_OPTIONS = 6
_ASK_MAX_QUESTION_LEN = 500
_ASK_MAX_HEADER_LEN = 50
_ASK_MAX_LABEL_LEN = 200
_ASK_MAX_DESC_LEN = 500
# The answer side is bounded too: answers are echoed verbatim into the agent's
# transcript, so an oversized custom answer would consume model context.
_ASK_MAX_ANSWER_LEN = 2000

# ask_question requests a NON-BLOCKING dashboard question card: the MCP tool
# returns a session directive and the agent ends its turn, so no tool call is
# held open (see mcp_tools.control.ask_question). `questions` is only
# shape-checked here (a bounded list); the per-question/per-option limits are
# enforced server-side by validate_ask_user_question, which is the single
# source of truth for the card payload.
ASK_QUESTION_SCHEMA = ToolSchema(
    tool_name="ask_question",
    fields=[
        FieldSpec("questions", list, required=True, max_items=_ASK_MAX_QUESTIONS),
        # Accepted but IGNORED: the directive the tool returns carries only
        # `questions`, so nothing downstream reads a timeout. The bound still
        # mirrors DashboardState._QUESTION_TIMEOUT_MAX, which governs the legacy
        # blocking POST /api/ask-question path. Kept lenient rather than removed
        # so a caller still passing it gets its card instead of a validation
        # error, while the tool's inputSchema no longer advertises it — a knob
        # with no effect should not be offered to a model.
        FieldSpec("timeout_secs", int, min_val=15, max_val=540),
    ],
)

# delete_message reads args["channel"] and args["ts"] by subscript. Without a
# schema, a call omitting either key raised KeyError, which is NOT caught by
# call_tool_with_logging (only ValidationError is) and propagated out of the
# stdio loop, killing the whole kirocrew-core MCP server for the session.
# Requiring both fields turns a missing key into a clean ValidationError string.
DELETE_MESSAGE_SCHEMA = ToolSchema(
    tool_name="delete_message",
    fields=[
        FieldSpec("channel", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("ts", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SKILL_SEARCH_SCHEMA = ToolSchema(
    tool_name="skill_search",
    fields=[
        FieldSpec("query", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("limit", int),
    ],
)

SKILL_DISCOVER_SCHEMA = ToolSchema(
    tool_name="skill_discover",
    fields=[
        FieldSpec("query", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("limit", int),
        FieldSpec("provider", str, max_len=MAX_SHORT_STRING),
    ],
)

# ``id`` is a provider path like "owner/repo/skill" — the provider itself
# rejects empty/./.. segments before it reaches a URL (see
# SkillsShProvider.fetch_skill_bundle), so this schema is the shape gate only.
SKILL_FETCH_SCHEMA = ToolSchema(
    tool_name="skill_fetch",
    fields=[
        FieldSpec("id", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("provider", str, max_len=MAX_SHORT_STRING),
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

# reset_conversation drops the calling session's model context at the next turn
# boundary. It takes NO arguments: a caller asking for a clean context always
# wants a clean one, and replaying the transcript into the fresh conversation
# returns most of what the reset reclaimed. The HTTP route
# (POST /api/chat/slots/{slot}/reset-conversation) still carries a replay flag
# for the rare caller that genuinely wants the provider reset without it.
RESET_CONVERSATION_SCHEMA = ToolSchema(
    tool_name="reset_conversation",
    fields=[],
)

# suggest_followup renders an agent-authored follow-up card in the calling
# dashboard slot. Every string below is LLM-authored and lands in the DOM and
# (for the worktree action) in a `git worktree add` argv, so the shapes are
# gated here at the MCP boundary rather than trusted downstream.
#
# Git branch grammar, deliberately narrower than git's own check-ref-format:
# must start alphanumeric, then alphanumerics / dot / underscore / hyphen /
# single slashes. This rejects the ref-name metacharacters that matter for the
# worktree action — leading "-" (which git would read as a flag), "..", "@{",
# "~", "^", ":", "?", "*", "[", "\", and whitespace — before the value ever
# reaches the endpoint. Anchored with \Z (not $) so a trailing newline cannot
# slip through. The endpoint re-validates; this is the first of two gates.
FOLLOWUP_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*\Z")

# The regex above is a character grammar, so four ref shapes still slip through
# it: ``foo..bar``, a component ending in ``.``, a component ending in ``.lock``,
# and the reserved name ``HEAD``. git rejects all four, but only AFTER the branch
# has been claimed and the destination derived — the user then sees a misleading
# "Branch already exists". Rejected up front
# instead, per component so ``feat/x.lock`` is caught as well as ``x.lock``.
_GIT_RESERVED_REFS = frozenset({"HEAD"})

# A branch is a loose ref FILE (`.git/refs/heads/<component>`), and Windows
# cannot create a file whose stem is a device name — so `feat/CON` claims fine
# but the checkout fails, surfacing as a false "Branch already exists". Rejected
# on every platform so the grammar does not depend on where the gateway runs.
# The stem vocabulary is shared with the app-name grammar; see
# ``constants.WINDOWS_DEVICE_STEMS``.


def is_valid_followup_branch(branch: str) -> bool:
    """Whether ``branch`` is a ref name git will actually accept."""
    if not branch or not FOLLOWUP_BRANCH_RE.match(branch):
        return False
    if ".." in branch or branch in _GIT_RESERVED_REFS:
        return False
    for part in branch.split("/"):
        if not part or part.endswith(".") or part.endswith(".lock"):
            return False
        # Device names are reserved with OR without an extension (CON, CON.txt).
        if part.split(".")[0].lower() in WINDOWS_DEVICE_STEMS:
            return False
    return True


MAX_FOLLOWUP_ITEMS = 3
MAX_FOLLOWUP_TITLE = 120
MAX_FOLLOWUP_DESCRIPTION = 600
# The handoff prompt is a full agent instruction, so it gets the same 8000-char
# ceiling as monitor_start's message rather than MAX_MEDIUM_STRING.
MAX_FOLLOWUP_PROMPT = 8_000
MAX_FOLLOWUP_BRANCH = 80


def _validate_followup_items(args: dict[str, Any]) -> None:
    """Validate + sanitize each follow-up item dict in place.

    ``validate_field`` only sanitizes *string* list elements, so a list of
    dicts arrives untouched. This walks each item, rejects unknown keys (same
    fail-closed posture as ``validate_tool_args``), enforces per-field types
    and lengths, and writes the sanitized values back into the dict so the
    caller receives cleaned content.
    """
    items = args.get("items")
    if not isinstance(items, list) or not items:
        raise ValidationError("items", "required (at least one follow-up item)")
    allowed_keys = {"title", "description", "prompt", "branch"}
    required_keys = ("title", "description", "prompt")
    limits = {
        "title": MAX_FOLLOWUP_TITLE,
        "description": MAX_FOLLOWUP_DESCRIPTION,
        "prompt": MAX_FOLLOWUP_PROMPT,
        "branch": MAX_FOLLOWUP_BRANCH,
    }
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError("items", f"item[{idx}]: expected object")
        for key in item:
            if key not in allowed_keys:
                raise ValidationError("items", f"item[{idx}]: unknown field {key!r}")
        for key in required_keys:
            raw = item.get(key)
            if not isinstance(raw, str):
                raise ValidationError("items", f"item[{idx}].{key}: required string")
            cleaned = sanitize_string(raw)
            if not cleaned:
                raise ValidationError("items", f"item[{idx}].{key}: required (empty)")
            if len(cleaned) > limits[key]:
                raise ValidationError(
                    "items",
                    f"item[{idx}].{key}: exceeds max length {limits[key]} "
                    f"(got {len(cleaned)}, trim {len(cleaned) - limits[key]} chars)",
                )
            item[key] = cleaned
        branch = item.get("branch")
        if branch is not None:
            if not isinstance(branch, str):
                raise ValidationError("items", f"item[{idx}].branch: expected string")
            branch = sanitize_string(branch)
            if not branch:
                # An explicitly-empty branch is treated as absent rather than
                # an error: the frontend derives a name from the title.
                item.pop("branch", None)
                continue
            if len(branch) > MAX_FOLLOWUP_BRANCH:
                raise ValidationError(
                    "items", f"item[{idx}].branch: exceeds max length {MAX_FOLLOWUP_BRANCH}"
                )
            if not is_valid_followup_branch(branch):
                raise ValidationError("items", f"item[{idx}].branch: invalid git branch name")
            item["branch"] = branch


SUGGEST_FOLLOWUP_SCHEMA = ToolSchema(
    tool_name="suggest_followup",
    fields=[
        FieldSpec("items", list, required=True, max_items=MAX_FOLLOWUP_ITEMS, item_type=dict),
    ],
    custom_validator=_validate_followup_items,
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
        FieldSpec("workflow", str, max_len=MAX_SHORT_STRING, pattern=_WF_RUN_ID_RE),
        FieldSpec("input", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("name", str, max_len=MAX_SHORT_STRING),
        FieldSpec("args", dict),
        FieldSpec("budget_total", int, min_val=0, max_val=100_000_000),
    ],
)

WORKFLOW_LIBRARY_LIST_SCHEMA = ToolSchema(
    tool_name="workflow_library_list",
    fields=[FieldSpec("search", str, max_len=MAX_MEDIUM_STRING)],
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
_ARTIFACT_KIND_RE = re.compile(r"^(widget|html|markdown|svg|json|text|image|webapp)$")

# Model identifiers passed to kiro-cli ``--model`` (AcpRuntime). First char
# must be alphanumeric so a value can never be parsed as a CLI flag, and the
# charset covers real model ids (gpt-5.6-sol, claude-sonnet-4.6) only.
MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
_ARTIFACT_SOURCE_RE = re.compile(r"^(chat|cron|subagent|manual|import)$")
# Single source of truth: the MCP save/update field cap MUST equal the store's
# own content cap, else the tool path rejects content the store would accept
# (or vice-versa). Import the store constant rather than re-declaring it.
from kiro_crew.artifacts import MAX_CONTENT_BYTES as ARTIFACT_CONTENT_MAX  # noqa: E402

ARTIFACT_WEBAPP_METADATA_MAX_BYTES = 16_384


def _validate_artifact_save(cleaned: dict) -> None:
    """Reject an oversized or structurally invalid webapp_metadata blob before disk write."""
    am = cleaned.get("webapp_metadata")
    if am is None:
        return
    if not isinstance(am, dict):
        raise ValidationError("webapp_metadata", "must be a dict")
    try:
        size = len(json.dumps(am, default=str))
    except (TypeError, ValueError) as exc:
        raise ValidationError("webapp_metadata", "is not JSON-serializable") from exc
    if size > ARTIFACT_WEBAPP_METADATA_MAX_BYTES:
        raise ValidationError(
            "webapp_metadata",
            f"serialized webapp_metadata exceeds {ARTIFACT_WEBAPP_METADATA_MAX_BYTES} bytes (got {size})",
        )
    # ── Bounded structural validation (input shape at MCP/HTTP boundary) ──
    _validate_webapp_metadata_shape(am)


# Shared slug pattern (matches _ARTIFACT_SLUG_RE + deploy slug validation).
_WM_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
# constants.AWS_PROFILE_NAME_RE — the single source of truth (#6063): '+'
# admitted for IAM Identity Center derived profiles
# ("<account>+<permission-set>", #6051); first char excludes '-' so a stored
# profile is never option-shaped when it later reaches `--profile <value>`
# argv. \Z is load-bearing here: this path matches the raw value WITHOUT a
# strip, so the old $ anchor let a trailing-newline value through.
_WM_PROFILE_RE = AWS_PROFILE_NAME_RE
_WM_URL_RE = re.compile(r"^https?://.{1,2048}$")
_WM_LIFECYCLE_STATUSES = {"draft", "deploying", "live", "error", "expired"}
_WM_LIST_CAP = 50


def _validate_webapp_metadata_shape(am: dict) -> None:
    """Validate webapp_metadata nested structure — tolerant for absent fields."""
    # app_dir — LLM-controlled filesystem path consumed by the local preview
    # channel. Reject control characters (NUL crashes
    # Path.resolve with ValueError) and relative paths at WRITE time; the
    # serve path re-validates against the allow-listed roots regardless.
    app_dir = am.get("app_dir")
    if app_dir is not None:
        if not isinstance(app_dir, str) or len(app_dir) > 4096:
            raise ValidationError("webapp_metadata.app_dir", "must be a string (max 4096 chars)")
        if app_dir != "":
            if any(ord(c) < 0x20 for c in app_dir):
                raise ValidationError(
                    "webapp_metadata.app_dir", "must not contain control characters"
                )
            posix_abs = app_dir.startswith("/") or app_dir.startswith("~/") or app_dir == "~"
            if not posix_abs and not PureWindowsPath(app_dir).is_absolute():
                raise ValidationError(
                    "webapp_metadata.app_dir", "must be an absolute path (or ~/-prefixed)"
                )
    # deploy_target.public_url
    dt = am.get("deploy_target")
    if dt is not None:
        if not isinstance(dt, dict):
            raise ValidationError("webapp_metadata.deploy_target", "must be a dict")
        pub_url = dt.get("public_url")
        if pub_url is not None:
            if not isinstance(pub_url, str):
                raise ValidationError(
                    "webapp_metadata.deploy_target.public_url",
                    "must be a valid http(s) URL (max 2048 chars)",
                )
            # Empty string is allowed (documented draft state — URL not yet assigned).
            if pub_url != "" and not _WM_URL_RE.match(pub_url):
                raise ValidationError(
                    "webapp_metadata.deploy_target.public_url",
                    "must be a valid http(s) URL (max 2048 chars)",
                )
            # Reject Basic-auth userinfo (https://user:pass@host) —
            # the regex above accepts it, and a credential-bearing URL would
            # be surfaced/linked by the dashboard, transmitting the embedded
            # credentials to the host when opened.
            if pub_url != "":
                from urllib.parse import urlsplit

                try:
                    parts = urlsplit(pub_url)
                except ValueError:
                    raise ValidationError(
                        "webapp_metadata.deploy_target.public_url",
                        "must be a valid http(s) URL (max 2048 chars)",
                    )
                if parts.username or parts.password:
                    raise ValidationError(
                        "webapp_metadata.deploy_target.public_url",
                        "must not contain userinfo (user:password@) credentials",
                    )
        # profile/region/slug — string-typed with existing regex patterns.
        for key, pattern, label in (
            ("profile", _WM_PROFILE_RE, "profile"),
            # Mirrors deploy/profiles.py _REGION_RE so a region the deploy path
            # accepted (incl. GovCloud us-gov-west-1) is never rejected here.
            ("region", re.compile(r"^[a-z]{2}(-[a-z]+)+-\d+$"), "region"),
            ("slug", _WM_SLUG_RE, "slug"),
        ):
            val = dt.get(key)
            if val is not None:
                if not isinstance(val, str) or len(val) > 128:
                    raise ValidationError(
                        f"webapp_metadata.deploy_target.{key}", "must be a string (max 128 chars)"
                    )
                if val and not pattern.match(val):
                    raise ValidationError(
                        f"webapp_metadata.deploy_target.{key}", f"invalid {label} format"
                    )

    # lifecycle.status enum
    lc = am.get("lifecycle")
    if lc is not None:
        if not isinstance(lc, dict):
            raise ValidationError("webapp_metadata.lifecycle", "must be a dict")
        status = lc.get("status")
        # Require a string BEFORE the enum membership test — an
        # unhashable value (list/dict) raises TypeError inside `in` and
        # turns artifact save/update into a 500.
        if status is not None and not isinstance(status, str):
            raise ValidationError("webapp_metadata.lifecycle.status", "must be a string")
        if status is not None and status not in _WM_LIFECYCLE_STATUSES:
            raise ValidationError(
                "webapp_metadata.lifecycle.status",
                f"must be one of {sorted(_WM_LIFECYCLE_STATUSES)}",
            )
        # persistent: strict bool — the string "false" is truthy and would
        # silently flip expiry/teardown behavior downstream.
        pers = lc.get("persistent")
        if pers is not None and not isinstance(pers, bool):
            raise ValidationError("webapp_metadata.lifecycle.persistent", "must be a boolean")
        # ttl_hours: strict int (bool excluded — bool subclasses int), 0-8760.
        ttl = lc.get("ttl_hours")
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, int):
                raise ValidationError("webapp_metadata.lifecycle.ttl_hours", "must be an integer")
            if not (0 <= ttl <= 8760):
                raise ValidationError("webapp_metadata.lifecycle.ttl_hours", "must be 0-8760")
        # expires_at ISO-8601 parseable
        exp = lc.get("expires_at")
        if exp is not None and exp != "":
            if not isinstance(exp, str):
                raise ValidationError("webapp_metadata.lifecycle.expires_at", "must be a string")
            from datetime import datetime, timezone

            try:
                datetime.strptime(exp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                raise ValidationError(
                    "webapp_metadata.lifecycle.expires_at",
                    "must be ISO-8601 UTC (YYYY-MM-DDTHH:MM:SSZ)",
                )

    # cost and architecture lists capped
    for key in ("cost", "architecture"):
        val = am.get(key)
        if val is not None:
            if isinstance(val, dict):
                # architecture.resources or cost.items may be lists inside
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, list) and len(sub_val) > _WM_LIST_CAP:
                        raise ValidationError(
                            f"webapp_metadata.{key}.{sub_key}",
                            f"list exceeds {_WM_LIST_CAP} entries",
                        )
            elif isinstance(val, list) and len(val) > _WM_LIST_CAP:
                raise ValidationError(
                    f"webapp_metadata.{key}", f"list exceeds {_WM_LIST_CAP} entries"
                )


ARTIFACT_SAVE_SCHEMA = ToolSchema(
    custom_validator=_validate_artifact_save,
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
        FieldSpec("webapp_metadata", dict),
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
    custom_validator=_validate_artifact_save,
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
        FieldSpec("webapp_metadata", dict),
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

# Artifact comments. Comment ids are local UUIDs or provider-origin
# ids (e.g. a remote provider's "<ts>-<uuid>"); allow alphanumerics + - . : _ .
_ARTIFACT_COMMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_ARTIFACT_SCOPE_RE = re.compile(r"^(private|shared)$")
ARTIFACT_COMMENT_TEXT_MAX = 10_000

#: Plain-text marker for agent-authored comments on CLI/text surfaces that lack
#: a structured ``is_agent`` flag (e.g. the ``artifact_get_comments`` MCP text
#: rendering). NOT persisted into the comment body and NOT an emoji: agent
#: provenance is carried by the structured ``is_agent`` field, which the
#: dashboard renders as a lucide ``Bot`` icon (no emoji in the UI, per
#: AGENTS.md). This constant is only for prefixing plain-text output.
ARTIFACT_AGENT_MARKER = "[agent] "

ARTIFACT_GET_COMMENTS_SCHEMA = ToolSchema(
    tool_name="artifact_get_comments",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
    ],
)

ARTIFACT_POST_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_post_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        # The body is stored verbatim (no watermark prepended), so the full
        # ARTIFACT_COMMENT_TEXT_MAX budget is available; agent provenance rides
        # on the structured is_agent field, not the text.
        FieldSpec("text", str, required=True, max_len=ARTIFACT_COMMENT_TEXT_MAX),
        FieldSpec("scope", str, max_len=10, pattern=_ARTIFACT_SCOPE_RE),
    ],
)

ARTIFACT_REPLY_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_reply_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("parent_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
        FieldSpec("text", str, required=True, max_len=ARTIFACT_COMMENT_TEXT_MAX),
    ],
)

ARTIFACT_MARK_REVIEW_SCHEMA = ToolSchema(
    tool_name="artifact_mark_review",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("comment_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
    ],
)

#: Cap on the one-line justification an agent must record when deleting a
#: comment it has applied (surfaced in the SEL audit + activity timeline).
ARTIFACT_DELETE_COMMENT_REASON_MAX = 500

ARTIFACT_DELETE_COMMENT_SCHEMA = ToolSchema(
    tool_name="artifact_delete_comment",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("comment_id", str, required=True, max_len=128, pattern=_ARTIFACT_COMMENT_ID_RE),
        FieldSpec("reason", str, required=True, max_len=ARTIFACT_DELETE_COMMENT_REASON_MAX),
    ],
)

# Artifact folders. A folder reference is a folder id OR a
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

# Chat (sidebar) folders. Same reference model as the artifact folders above —
# a folder is addressed by id OR by a ``/``-separated human path — so the two
# bounds are shared. Folder names cap at 100 chars server-side
# (chat_folders.api_chat_folder_create truncates); reject longer input here
# instead of silently filing the session under a truncated name.
CHAT_FOLDER_TREE_SCHEMA = ToolSchema(
    tool_name="chat_folder_tree",
    fields=[],
)

CHAT_FOLDER_CREATE_SCHEMA = ToolSchema(
    tool_name="chat_folder_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=_ARTIFACT_FOLDER_NAME_MAX),
        FieldSpec("parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_FOLDER_MOVE_SCHEMA = ToolSchema(
    tool_name="chat_folder_move",
    fields=[
        FieldSpec("folder", str, required=True, max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("new_parent", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

CHAT_FOLDER_MOVE_SESSION_SCHEMA = ToolSchema(
    tool_name="chat_folder_move_session",
    fields=[
        # A session reference is a slot key, a ``dashboard:`` session key, or an
        # exact session title — none share a charset, so only bound the length.
        FieldSpec("session", str, required=True, max_len=512),
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

ARTIFACT_MOVE_SCHEMA = ToolSchema(
    tool_name="artifact_move",
    fields=[
        FieldSpec("slug", str, required=True, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("folder", str, max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

DEPLOY_ARTIFACT_SCHEMA = ToolSchema(
    tool_name="deploy_artifact",
    fields=[
        FieldSpec("site_id", str, required=True, max_len=64),
        FieldSpec("artifact_slug", str, max_len=80, pattern=_ARTIFACT_SLUG_RE),
        FieldSpec("local_dir", str, max_len=4096),
        FieldSpec("profile", str, max_len=64),
        FieldSpec("ttl_hours", int, min_val=0, max_val=8760),
    ],
)

# ── Tool Schemas (Issue Radar app) ──
#
# ``issue_radar_record_investigation`` is the agent's ONLY write path into an
# Issue Radar investigation record. The findings sub-object is flattened into
# per-field args on purpose: ``FieldSpec`` validates scalars and string lists,
# not nested dicts, so a single ``findings`` dict arg would reach the gateway
# unvalidated. The tool re-assembles the object from these fields.

_ISSUE_RADAR_PROVIDERS = frozenset({"github", "gitlab"})
_ISSUE_RADAR_ITEM_KINDS = frozenset({"issue", "pull"})
_ISSUE_RADAR_STATUSES = frozenset({"investigating", "resolved", "archived"})
# Mirrors ``issue_radar.backend.routes.MAX_ITEM_NUMBER``: bounds the number that
# becomes part of the record's FILENAME (``investigation-<n>.json``), so an
# absurd value cannot produce an ENAMETOOLONG write.
_ISSUE_RADAR_MAX_ITEM_NUMBER = 1_000_000_000

# ── Tool Schemas (Ops Mission Control app) ──
#
# ``ops_mission_control_api`` is the agent's ONLY credentialed path to the
# app's HTTP surface (same pattern as ``issue_radar_record_investigation``:
# the MCP server process holds the internal secret; the agent never sees a
# credential). The (method, path) allowlist below is the entire authorization
# story for the tool, so it is defined here — next to the schema that
# enforces it — and imported by both the tool handler and the tests. It
# deliberately covers only what the app's SOPs need and EXCLUDES the
# human-decision and configuration routes (``/incident/proposal/decide``,
# ``/incident/propose``, ``/proposals``, ``/providers*``, ``/settings``,
# ``/webhook``, bare ``/incident``): an agent that needs one of those is by
# definition off-SOP.

OPS_MISSION_CONTROL_ALLOWED_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/state"),
        ("GET", "/signals"),
        ("GET", "/incidents"),
        ("GET", "/handover"),
        ("GET", "/rotation"),
        ("GET", "/ledger"),
        ("GET", "/ledger/contradictions"),
        ("POST", "/dispatch"),
        ("POST", "/incident/transition"),
        ("POST", "/incident/claim"),
        ("POST", "/incident/action"),
        ("POST", "/rotation/arm"),
        ("POST", "/ledger"),
        ("POST", "/ledger/hygiene"),
    }
)
_OMC_API_METHODS = frozenset({"GET", "POST"})
_OMC_API_PATHS = frozenset(p for _, p in OPS_MISSION_CONTROL_ALLOWED_CALLS)
# Query strings are value-position only: no '/', '?' or '#', so a query can
# never rewrite the path it is appended to. '%' admits URL-encoded values.
_OMC_QUERY_RE = re.compile(r"^[A-Za-z0-9_.=&%+,:-]*$")
# Bounds the JSON body an agent can push through the tool. Ledger entries are
# the largest legitimate payload; 32 KiB is ~4x the biggest one observed.
_OMC_MAX_BODY = 32_768


def _validate_omc_api(cleaned: dict[str, Any]) -> None:
    method = cleaned.get("method")
    path = cleaned.get("path")
    if (method, path) not in OPS_MISSION_CONTROL_ALLOWED_CALLS:
        raise ValidationError("path", f"{method} {path} is not part of the agent surface")
    if cleaned.get("query") and method != "GET":
        raise ValidationError("query", "query is only accepted on GET calls")
    if cleaned.get("body_json") and method == "GET":
        raise ValidationError("body_json", "GET calls take no body")


OPS_MISSION_CONTROL_API_SCHEMA = ToolSchema(
    tool_name="ops_mission_control_api",
    fields=[
        FieldSpec("method", str, required=True, allowed=_OMC_API_METHODS),
        FieldSpec("path", str, required=True, allowed=_OMC_API_PATHS),
        FieldSpec("query", str, max_len=512, pattern=_OMC_QUERY_RE, default=""),
        FieldSpec("body_json", str, max_len=_OMC_MAX_BODY, default=""),
    ],
    custom_validator=_validate_omc_api,
)

ISSUE_RADAR_RECORD_INVESTIGATION_SCHEMA = ToolSchema(
    tool_name="issue_radar_record_investigation",
    fields=[
        FieldSpec("owner", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("repo", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("number", int, required=True, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        # provider/host/kind are REQUIRED, with no defaults, because together
        # with owner/repo they select the record's STORAGE NAMESPACE (see
        # ``store.provider_root``: public GitHub keeps the original
        # ``repos/{owner}/{repo}`` path, everything else lives under the
        # ``@providers`` subtree). Defaulting them to GitHub would let a caller
        # that simply omits the identity — a GitLab investigation, say — write
        # into the GitHub ledger and silently overwrite a same-slug GitHub
        # record, since a GitLab group can share a name with a GitHub owner.
        # The caller always has this information to hand (`recordIdentityJson`
        # in ``website/src/apps/issue-radar/lib/links.ts`` emits all three), so
        # requiring them costs nothing and removes the ambiguity.
        FieldSpec("provider", str, required=True, max_len=16, allowed=_ISSUE_RADAR_PROVIDERS),
        FieldSpec("host", str, required=True, max_len=253),
        FieldSpec("kind", str, required=True, max_len=8, allowed=_ISSUE_RADAR_ITEM_KINDS),
        FieldSpec("status", str, max_len=16, allowed=_ISSUE_RADAR_STATUSES, default="resolved"),
        FieldSpec("verdict", str, max_len=MAX_SHORT_STRING),
        FieldSpec("root_cause", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("next_action", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("summary", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec(
            "suggested_labels",
            list,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            max_items=20,
        ),
    ],
)

# ── Tool Schemas (Issue Radar crews) ──
#
# The crew ledger is an autonomous agent's ONLY memory across compaction, the
# per-turn ceiling and a gateway restart, so both tools are on the same
# internal-secret path as ``issue_radar_record_investigation`` and validated
# here, next to it.
#
# Neither schema carries owner/repo/crew_id. That is a security choice, not an
# omission: identity is resolved from the CALLING SESSION (see
# ``mcp_core._crew_identity``). If the model could name the crew, a crew could
# write into another crew's ledger — clobbering its ``next``, its worktree path
# or its claim comment id — and the store's "at most one item in an editing
# phase" invariant is per-crew, so a cross-crew write would also defeat that.
#
# The phase / event-kind vocabularies MIRROR
# ``issue_radar.backend.crew_store.PHASES`` and ``.EVENT_KINDS`` rather than
# importing them: ``validation`` is core and must not import an app package
# (apps load dynamically and may be absent). ``test_issue_radar_crew_mcp_tools``
# asserts the mirrors are exact, so drift fails a test instead of silently
# rejecting a legitimate phase at the tool boundary.
_ISSUE_RADAR_CREW_PHASES = frozenset(
    {
        "selected",
        "claimed",
        "investigating",
        "implementing",
        "awaiting-ci",
        "addressing-review",
        "awaiting-merge",
        "awaiting-reply",
        "resolved",
        "skipped",
        "yielded",
        "handed-back",
        "preempted",
    }
)
_ISSUE_RADAR_CREW_EVENT_KINDS = frozenset(
    {
        "claim",
        "investigate",
        "reply",
        "implement",
        "ci",
        "review",
        "conflict",
        "merge",
        "handback",
        "skip",
        "yield",
        # The one crew-level kind: the crew looked at the queue and took nothing.
        # It is the only kind valid with NO ``number``, and it is invalid WITH one
        # — a relation between two fields, so it is enforced on the write route
        # and in the store rather than here.
        "sweep",
    }
)
#: Mirrors ``crew_store.SKIP_SCOPES`` — the classification a crew attaches to a
#: pass in the repo-wide shared skip index. Advertised to the model as an enum so
#: it picks a real one; NOT enforced as ``allowed=`` on the field (see the
#: ``skip_scope`` spec below for why).
#:
#: ``needs-decision`` and ``needs-investigation`` are how a crew says the next step
#: belongs to a human. They are scopes on a PASS because a crew never holds an
#: issue waiting for one: it says what it needs on the issue, labels it, records the
#: pass and moves on.
_ISSUE_RADAR_CREW_SKIP_SCOPES = frozenset(
    {
        "architecture",
        "new-feature",
        "needs-design",
        "needs-decision",
        "needs-investigation",
        "duplicate",
        "already-fixed",
        "not-reproducible",
        "wrong-root-cause",
        "breaking-change",
        "gate-config",
        "other",
    }
)

# Abbreviated-or-full git object name. Bounds ``base_sha`` to something that can
# actually be handed to git on a resume; a resumed turn checks out from this
# value, so an arbitrary 5k string here is a resume that fails much later.
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _validate_crew_record_couples_phase_to_an_event(args: dict[str, Any]) -> None:
    """Enforce the invariants that justify ONE write tool instead of two.

    * ``event`` and ``event_kind`` travel together — the store refuses an
      unknown kind, and an event with no kind cannot be filed on either surface
      it feeds (crew page + the public claim comment).
    * a ``phase`` write must carry its reason. Splitting upsert and append into
      two tools is what allows a phase to move with nothing logged; merging them
      only closes that if the event is actually mandatory on a phase change.
    """
    if args.get("event") and not args.get("event_kind"):
        raise ValidationError("event_kind", "required when 'event' is given")
    if args.get("event_kind") and not args.get("event"):
        raise ValidationError("event", "required when 'event_kind' is given")
    if args.get("phase") and not args.get("event"):
        raise ValidationError(
            "event",
            "required when 'phase' changes — record the reason with the phase "
            "(also pass 'event_kind')",
        )


ISSUE_RADAR_CREW_READ_SCHEMA = ToolSchema(
    tool_name="issue_radar_crew_read",
    fields=[
        # Deliberately empty: no argument can select WHICH crew is read (see the
        # block comment above). ``max_events`` is not exposed either — the
        # handler bounds the log itself so a long-lived crew cannot blow the
        # caller's context by asking for more.
    ],
)

ISSUE_RADAR_CREW_RECORD_SCHEMA = ToolSchema(
    tool_name="issue_radar_crew_record",
    fields=[
        # Bounds the number that becomes the work item's FILENAME
        # (``crews/<crew_id>/<n>.json``) — same ENAMETOOLONG rationale as the
        # investigation record, hence the same constant.
        #
        # NOT required. A crew that swept its queue and took nothing has no issue
        # to name, and requiring one here left it recording the cycle against an
        # issue it never acted on. The coupling that replaces the requirement —
        # a missing number is valid ONLY with the crew-level ``sweep`` kind, and
        # ``sweep`` is valid ONLY without one — is enforced on the write route and
        # in the store, because it is a relation between two fields and this
        # schema validates them one at a time. Keeping the bound here still
        # matters: when a number IS sent it is the filename.
        FieldSpec("number", int, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        FieldSpec("phase", str, max_len=32, allowed=_ISSUE_RADAR_CREW_PHASES),
        # Bounded but deliberately NOT ``allowed=``, unlike ``phase`` beside it.
        # An out-of-vocabulary phase has to be refused — it would corrupt the
        # phase state machine. A scope is only a filter label, and refusing one
        # would fail the whole write, which on a ``skipped`` write is the write
        # that puts the issue in the shared skip index. Weakening "a skip is
        # always indexed" to buy a tidier label is the wrong trade, so the store
        # coerces an unknown value to ``other`` (``crew_store.SKIP_SCOPES``) and
        # the pass is recorded either way. The vocabulary is still advertised as
        # an enum in the tool schema, so the model is told what to pick.
        FieldSpec("skip_scope", str, max_len=32),
        # ``outcome`` is a bounded free string, NOT an enum: the store keeps it
        # as free text (``crew_store.upsert_work_item``) and no vocabulary is
        # defined anywhere in the app, so an allowlist invented here would
        # reject a legitimate terminal outcome and lose it.
        FieldSpec("outcome", str, max_len=MAX_SHORT_STRING),
        FieldSpec("next", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("decision", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("why", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("tried_approach", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("tried_rejected_because", str, max_len=MAX_MEDIUM_STRING),
        # Local-only resume fields. These are the ONE place an absolute path
        # legitimately belongs, which is why the handler must not scrub them the
        # way it scrubs the public strings.
        FieldSpec("worktree", str, max_len=4096),
        FieldSpec("branch", str, max_len=255),
        FieldSpec("base_sha", str, max_len=64, pattern=_GIT_SHA_RE),
        FieldSpec("pr_number", int, min_val=1, max_val=_ISSUE_RADAR_MAX_ITEM_NUMBER),
        # ci_* are flat args assembled into the store's ``ci_state`` dict by the
        # handler. ``ci_state`` is the forge's own verdict word (success /
        # failure / pending / neutral / cancelled / timed_out, and GitLab's
        # differ again), so it is bounded but not enumerated here.
        FieldSpec("ci_state", str, max_len=32),
        FieldSpec("ci_passed", int, min_val=0, max_val=100_000),
        FieldSpec("ci_total", int, min_val=0, max_val=100_000),
        FieldSpec("ci_round", int, min_val=0, max_val=1_000),
        FieldSpec("ci_inherited_reds", int, min_val=0, max_val=100_000),
        # Forge comment ids are large (GitHub is past 3e9 and monotonic).
        FieldSpec("claim_comment_id", int, min_val=1, max_val=10**18),
        # No ``crew:``-prefix pattern here on purpose: this field RECORDS what
        # was applied so a hand-back knows what to remove. The prefix allowlist
        # belongs to the forge write route that applies a label; enforcing it at
        # this boundary would reject a truthful record and lose the removal list.
        FieldSpec(
            "labels_applied",
            list,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            max_items=20,
        ),
        # One public progress line. Short by design: it is rendered as a list
        # item inside the claim comment's <details> block, not as a report.
        FieldSpec("event", str, max_len=MAX_SHORT_STRING),
        FieldSpec("event_kind", str, max_len=16, allowed=_ISSUE_RADAR_CREW_EVENT_KINDS),
    ],
    custom_validator=_validate_crew_record_couples_phase_to_an_event,
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
        FieldSpec("message", str, max_len=MAX_CRON_MESSAGE),
        FieldSpec("every", int, min_val=60, max_val=86400 * 30),
        FieldSpec("cron_expr", str, max_len=100),
        FieldSpec("at", (int, float), min_val=0, max_val=4102444800),  # up to 2100
        FieldSpec("delay", (int, float), min_val=1, max_val=86400 * 30),  # 1s to 30 days
        FieldSpec("at_time", str, max_len=100),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
        FieldSpec("model", str, max_len=MAX_SHORT_STRING, pattern=_MODEL_NAME_RE),
        FieldSpec("silent", bool),
        FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
        FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
        FieldSpec("approval_mode", str, max_len=10, pattern=re.compile(r"^(auto)?$")),
        FieldSpec(
            "skip_dates",
            list,
            item_type=str,
            item_max_len=10,
            max_items=366,
            item_pattern=re.compile(r"^\d{4}-\d{2}-\d{2}$"),
        ),
        FieldSpec("timezone", str, max_len=50, pattern=re.compile(r"^[A-Za-z0-9_/+-]+$")),
        FieldSpec("persistent_session", bool),
        FieldSpec("minimal_context", bool),
        FieldSpec("hide_in_chat", bool),
        FieldSpec("strict_schedule", bool),
        # SECURITY NOTE: the patterns below are input-SHAPE checks, NOT security
        # sanitizers. The "command" regex only rejects control bytes and the
        # "script" regex only enforces a path:func shape — neither makes the
        # value safe to execute. The enforced security boundary for the
        # model-supplied cron command/script lives elsewhere:
        #   1. storage-time deny-list  -> mcp_cron._vet_shell_command / _vet_script_file
        #   2. exec-time OS sandbox     -> cron_script.run_command_sandboxed (mode="cc")
        #                                  + _clean_cron_env() env scrubbing
        # Do not treat these regexes as the guard, and do not relax them assuming
        # downstream code re-validates the value as safe.
        # The shape is "<path>:<func>". The path allows backslash, spaces and an
        # OPTIONAL leading "<letter>:" Windows drive prefix, so a real Windows
        # absolute path validates — the old class omitted "\", ":" and " ", which
        # rejected every path Explorer produces AND made a script cron
        # impossible for the default "First Last" Windows account (config_dir()
        # is rooted at %USERPROFILE%, so the only legal crons dir was
        # unrepresentable). A leading "\\" (UNC) is excluded: it is not a local
        # path, and resolving one triggers an outbound SMB/DNS probe.
        # The trailing ":<func>" is still required and unambiguous — the drive
        # colon is at index 1 followed by a separator, the func colon is last and
        # followed by an identifier; resolve_script_path splits drive-aware.
        FieldSpec(
            "script",
            str,
            max_len=200,
            pattern=re.compile(
                r"^(?![\\/]{2})(?:[a-zA-Z]:)?[a-zA-Z0-9 _.~/\\-]+:[a-zA-Z_][a-zA-Z0-9_]*$"
            ),
        ),
        FieldSpec("command", str, max_len=5000, pattern=re.compile(r"^[^\x00-\x1f\x7f]*$")),
        FieldSpec("timeout", int, min_val=0, max_val=3600),
        FieldSpec("timeout_secs", int, min_val=1, max_val=86400),
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


def _validate_hook_has_action(args: dict) -> None:
    """A hook must have either a command or skills — an empty hook is invalid."""
    if not args.get("command") and not args.get("skills"):
        raise ValidationError("command", "either command or skills must be provided")
    # Skills injection only fires for a *standalone* skills hook (no command) on
    # UserPromptSubmit/AgentSpawn — see ScriptHookStore.fire(), which emits the
    # "Load skills:" directive only when `hook.skills and not hook.command` and
    # the event is one of those two. Reject any other pairing at save time so a
    # hook can never save cleanly and then silently never fire:
    #   - skills + a command: the command runs but the skills are inert.
    #   - skills on PreToolUse/PostToolUse/Stop: the directive has no consumer.
    if args.get("skills"):
        if args.get("command"):
            raise ValidationError(
                "skills",
                "skills cannot be combined with a command — the skills would "
                "never fire; use a skills-only hook or drop the skills",
            )
        event = args.get("event", "")
        if event in ("PreToolUse", "PostToolUse", "Stop"):
            raise ValidationError(
                "skills",
                f"skills hooks cannot fire on {event} events — "
                "choose UserPromptSubmit or AgentSpawn",
            )


def _validate_hook_create(args: dict) -> None:
    """Validate a hook at creation time: action + regex syntax."""
    _validate_hook_has_action(args)
    _validate_hook_regex(args)


def _validate_hook_update(args: dict) -> None:
    """Validate a hook at update time: regex syntax (action already checked by store)."""
    _validate_hook_regex(args)


def _validate_hook_regex(args: dict) -> None:
    """Reject an invalid regex pattern at save time with a field-level error."""
    matcher = args.get("matcher", "")
    mode = args.get("matcher_mode", "glob")
    if mode == "regex" and matcher:
        try:
            re.compile(matcher)
        except re.error as exc:
            raise ValidationError("matcher", f"invalid regex: {exc}") from None


HOOK_CREATE_SCHEMA = ToolSchema(
    tool_name="hook_create",
    fields=[
        FieldSpec("name", str, required=True, max_len=200),
        FieldSpec("command", str, max_len=2000, default=""),
        FieldSpec("event", str, required=True, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500, default=""),  # optional: empty = match all
        FieldSpec(
            "matcher_mode",
            str,
            max_len=10,
            default="glob",
            allowed=frozenset({"glob", "regex", "contains"}),
        ),
        FieldSpec("skills", list, default=[], item_type=str, item_max_len=100),
        FieldSpec("timeout", int, min_val=1, max_val=300, default=30),
        FieldSpec("enabled", bool, default=True),
    ],
    custom_validator=_validate_hook_create,
)

HOOK_UPDATE_SCHEMA = ToolSchema(
    tool_name="hook_update",
    fields=[
        FieldSpec("name", str, max_len=200),  # optional on update
        FieldSpec("command", str, max_len=2000),  # optional on update
        FieldSpec("event", str, allowed=ALLOWED_HOOK_EVENTS),
        FieldSpec("matcher", str, max_len=500),  # optional: empty = match all
        FieldSpec(
            "matcher_mode", str, max_len=10, allowed=frozenset({"glob", "regex", "contains"})
        ),
        FieldSpec("skills", list, item_type=str, item_max_len=100),
        FieldSpec("timeout", int, min_val=1, max_val=300),
        FieldSpec("enabled", bool),
    ],
    custom_validator=_validate_hook_update,
)

# ── Tool Schemas (File I/O) ──

#: Syntactic shape gate for a filesystem path arriving over the dashboard's
#: file endpoints. It admits POSIX (``/x``, ``~/x``) *and* native Windows
#: (``C:\x``, ``C:/x``, UNC ``\\host\share\x``) absolute paths. A prefix is
#: still required, so a bare relative path is refused exactly as before --
#: the endpoints that support relative input rewrite it to an absolute path
#: via ``_resolve_project_relative`` under ``resolve=1``, ahead of this gate.
#:
#: One pattern rather than a ``sys.platform`` branch: a drive letter and a UNC
#: root have no meaning on POSIX, so accepting those shapes there admits no
#: path that was previously unreachable, and a single pattern cannot drift
#: between platforms the way two would. This is a *syntax* gate only -- the
#: security boundary is downstream, where ``hooks.validate_file_path``
#: canonicalizes through ``realpath`` (resolving ``..`` and following symlinks)
#: and refuses the resolved target via ``is_sensitive_path``.
#:
#: The prefix alternation is matched separately from the body so that ``:``
#: stays confined to a drive prefix: an NTFS alternate data stream
#: (``C:\x\file.txt:hidden``, which reads a different byte stream than the path
#: the caller appears to name) has a ``:`` in the body and is still refused.
#: A drive-relative path (``C:x``) is likewise refused -- it resolves against a
#: per-drive working directory the caller cannot see.
_FS_PATH_PATTERN = re.compile(r"^(?:[~/]|[A-Za-z]:[\\/]|\\\\)[-\w.@~/\\ ]+$")

FILE_READ_SCHEMA = ToolSchema(
    tool_name="file_read",
    fields=[
        FieldSpec("path", str, required=True, max_len=4096, pattern=_FS_PATH_PATTERN),
    ],
)

FILE_WRITE_SCHEMA = ToolSchema(
    tool_name="file_write",
    fields=[
        FieldSpec("path", str, required=True, max_len=4096, pattern=_FS_PATH_PATTERN),
        FieldSpec("content", str, required=True, max_len=512000),
    ],
)

# Non-Slack channel routing for send_message. `validate_tool_args` REJECTS an
# unknown field, so a property advertised in the MCP inputSchema but missing here
# is not merely unvalidated — the whole call fails, and the capability is 0%
# reachable over MCP. The two must be added together.
#
# The channel is matched by SHAPE, not against an enumeration: the authoritative
# set is which transports are registered and what channels governance permits,
# both checked at send time, and a literal list here would be a second copy that
# goes stale the moment a channel is added. The pattern itself lives on the
# ``channel_type`` FieldSpec below, so there is exactly one of it.
#
# A configured-destination id is opaque and channel-defined — a Webex room id is
# a ~90-char base64 Hydra blob — so this bounds length and excludes control
# characters and whitespace rather than pretending to know the grammar. The id is
# re-resolved against the channel's own configured targets before any send, which
# is what actually authorizes it.
_TARGET_ID_RE = re.compile(r"^[\x21-\x7e]{1,512}$")

# Every legal ``send_message`` ``session`` value: the delivery MODES plus every
# channel a proactive send may name. Built from the shared
# ``CHANNEL_SEND_NAMESPACES`` so this, the tool's advertised enum and the gateway's
# accepted ``channel_type`` set are three views of ONE roster rather than three
# lists that drift -- which is the #6514 defect, where this pattern and the tool's
# enum both still read "discord" alone months after eight more transports were
# registered and made serviceable by the gateway's channel-neutral owner-DM leg.
#
# ``origin`` is added because it is a delivery MODE rather than a transport, and
# ``slack`` because it routes through its own client instead of the channel ladder;
# both are outside the send roster for those reasons, not by omission.
_SEND_MESSAGE_SESSION_RE = re.compile(
    "^(origin|" + "|".join((SLACK_NAMESPACE, *CHANNEL_OWNER_DM_NAMESPACES)) + ")$"
)


# Fields that select or shape a SLACK delivery. Combined with the
# ``channel_type``/``target_id`` pair — which addresses a non-Slack destination
# directly — they have no destination, so the pair's handler drops them before
# any Slack-shaped validation runs. Refused here at the boundary rather than
# dropped, because the caller (including the model) cannot observe a drop and
# would read a private channel DM as a threaded post to a named Slack channel.
_CHANNEL_TARGET_INCOMPATIBLE = (
    "channel",
    "user",
    "blocks",
    "thread_ts",
    "reply_broadcast",
    "unfurl_links",
    "unfurl_media",
    "session",
)


def _validate_channel_routing(cleaned: dict[str, Any]) -> None:
    """``target_id`` is meaningful only ALONGSIDE ``channel_type``.

    ``channel_type`` alone is complete on its own: it means the non-Slack
    conversation this session already belongs to. Adding ``target_id`` narrows that
    transport to one explicit configured destination on it. So the only
    under-specified combination is a ``target_id`` with no transport to resolve it
    against, and it is rejected at the boundary rather than ignored downstream,
    where it would silently fall back to the default Slack/dashboard destination.

    A Slack-routing field travelling with ``channel_type`` is likewise refused, not
    dropped: the channel wins the routing, so the field would reach nothing.
    """
    has_channel = bool(cleaned.get("channel_type"))
    if bool(cleaned.get("target_id")) and not has_channel:
        raise ValidationError("channel_type", "target_id requires channel_type")
    if has_channel:
        stray = [f for f in _CHANNEL_TARGET_INCOMPATIBLE if cleaned.get(f) is not None]
        if stray:
            raise ValidationError(
                stray[0],
                "channel_type/target_id addresses the destination directly and cannot be "
                f"combined with the Slack-routing field(s): {', '.join(stray)}",
            )


SEND_MESSAGE_SCHEMA = ToolSchema(
    tool_name="send_message",
    fields=[
        FieldSpec("text", str, required=True, max_len=MAX_MEDIUM_STRING),
        # Non-Slack transport name. Shape-only here (the cheap first gate, same
        # role as SEND_NOTIFICATION's `url` pattern); the authoritative closed set
        # is `_SEND_MESSAGE_CHANNEL_TYPES` in dashboard/handlers/messaging.py,
        # derived from `CHANNEL_SESSION_NAMESPACES`. Enumerating it here too would
        # be a second copy that goes stale when a transport is added.
        #
        # A BARE transport name, so no digits and no separator: that is what keeps a
        # session key or a namespaced value (`telegram:99887766`) from arriving where
        # a transport is expected. Declared ONCE, beside the ``target_id`` it pairs
        # with -- ``validate_tool_args`` ITERATES this list rather than indexing it,
        # so a second ``channel_type`` spec is not an alternative: a value would have
        # to satisfy both and the later one would overwrite ``cleaned``, silently
        # making the first pattern dead.
        FieldSpec("channel_type", str, max_len=16, pattern=re.compile(r"^[a-z]+$")),
        FieldSpec("target_id", str, max_len=512, pattern=_TARGET_ID_RE),
        FieldSpec("title", str, max_len=MAX_SHORT_STRING),
        FieldSpec("blocks", list, item_type=dict, max_items=50),
        FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
        FieldSpec("user", str, max_len=USER_MAX_LEN, pattern=USER_ID_RE),
        FieldSpec("unfurl_links", bool),
        FieldSpec("unfurl_media", bool),
        FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
        FieldSpec("reply_broadcast", bool),
        # Must accept every value ``mcp_tools.messaging._SESSION_TARGETS``
        # advertises: this pattern runs BEFORE the handler, so a value missing
        # here is rejected as malformed even though the tool's own enum offers
        # it. That is what happened to the eight non-Discord channels in #6514.
        #
        # DERIVED from the same roster the tool's enum and the gateway's accepted
        # ``channel_type`` set are built from, so the three cannot disagree.
        # Importing it costs no cycle: the roster is homed in ``kiro_crew.constants``,
        # which imports only ``os`` and ``re``. It is deliberately NOT read from
        # ``messaging.link`` (which re-exports it): importing a name from there
        # executes ``messaging/__init__.py``, pulling in ``driver`` -> ``acp`` ->
        # ``hooks``, and ``hooks`` -> ``webhooks`` -> ``validation`` is already an
        # edge, so reading it from this module that way closes a startup cycle.
        # ``unified`` is excluded because it is a session-key bucket, not a
        # transport; ``origin`` is added because it is a delivery MODE.
        FieldSpec(
            "session",
            str,
            max_len=MAX_SHORT_STRING,
            pattern=_SEND_MESSAGE_SESSION_RE,
        ),
        FieldSpec("caller_session", str, max_len=MAX_SHORT_STRING, pattern=CRON_SESSION_RE),
    ],
    custom_validator=_validate_channel_routing,
)

SEND_NOTIFICATION_SCHEMA = ToolSchema(
    tool_name="send_notification",
    fields=[
        FieldSpec("title", str, required=True, max_len=MAX_SHORT_STRING),
        # Cap matches the bus's _MAX_BODY_LEN (20000) so the schema never
        # rejects a body the advertised contract accepts.
        FieldSpec("body", str, max_len=20_000),
        FieldSpec("priority", str, max_len=16, pattern=re.compile(r"^(critical|default|passive)$")),
        # Path-only deep link -- the gateway re-validates with the full
        # WHATWG-hardened rule at the persistence trust root; this pattern
        # is the cheap first gate (must start with '/', not '//').
        FieldSpec("url", str, max_len=500, pattern=re.compile(r"^/(?!/)\S*$")),
        FieldSpec("group_key", str, max_len=MAX_SHORT_STRING),
        # Inline actions (Phase 4 contract). Item cap mirrors the bus's
        # _MAX_ACTIONS (4); per-item shape ({id,label,url?}, length caps,
        # internal-path url) is validated by NotificationPayload at the
        # persistence trust root -- this schema bounds the outer list so a
        # malformed MCP call is rejected before the HTTP hop.
        FieldSpec("actions", list, item_type=dict, max_items=4),
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

# select_crew: `crew` is optional — omitted/empty returns the roster. When
# present it is NOT pattern-validated here: crew creation only strips the name
# (agents.py), so names may contain spaces/dots; the deny-by-default gate is the
# `crew not in cfg.agents` membership check in _do_select_crew, not a regex.
SELECT_CREW_SCHEMA = ToolSchema(
    tool_name="select_crew",
    fields=[
        FieldSpec(
            "crew",
            str,
            required=False,
            max_len=MAX_SHORT_STRING,
            default="",
        ),
    ],
)

# ── Slack reaction field patterns ──

# Slack emoji names: alphanumeric, underscores, hyphens, and plus signs
_EMOJI_NAME_RE = re.compile(r"^[a-zA-Z0-9+\-][a-zA-Z0-9_+\-]{0,98}[a-zA-Z0-9]$|^[a-zA-Z0-9+]$")
# Slack message timestamp: digits.digits
_SLACK_TS_RE = re.compile(r"^\d+\.\d+$")

LOCAL_KNOWLEDGE_SEARCH_SCHEMA = ToolSchema(
    tool_name="local_knowledge_search",
    fields=[
        FieldSpec("query", str, required=True, max_len=500),
        FieldSpec("limit", int, required=False, min_val=1, max_val=5, default=3),
        # Opaque source id (uuid4 today). No pattern: an unknown id must reach
        # the handler for a graceful "use knowledge_list_sources" reply, not a
        # ValidationError; every downstream use is a parameterized SQL bind.
        FieldSpec("source_id", str, required=False, max_len=64),
    ],
)

KNOWLEDGE_LIST_SOURCES_SCHEMA = ToolSchema(
    tool_name="knowledge_list_sources",
    fields=[],
)

KNOWLEDGE_DEDUP_SCHEMA = ToolSchema(
    tool_name="knowledge_dedup",
    fields=[
        FieldSpec("apply", bool, required=False, default=False),
    ],
)

KNOWLEDGE_ADD_DOCUMENT_SCHEMA = ToolSchema(
    tool_name="knowledge_add_document",
    fields=[
        FieldSpec("title", str, required=True, max_len=200),
        FieldSpec("content", str, required=True, max_len=2_000_000),
        FieldSpec("reason", str, required=False, max_len=500),
        FieldSpec("source_uri", str, required=True, max_len=1024),
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

LIST_SESSIONS_SCHEMA = ToolSchema(
    tool_name="list_sessions",
    fields=[
        FieldSpec("limit", int, required=False, min_val=1, max_val=100, default=20),
        FieldSpec("all_workspaces", bool, required=False, default=False),
        FieldSpec("summarize", bool, required=False, default=False),
    ],
)

SESSION_CREATE_SCHEMA = ToolSchema(
    tool_name="session_create",
    fields=[
        FieldSpec("title", str, required=False, default="", max_len=200),
        FieldSpec("agent", str, required=False, default="", max_len=MAX_SHORT_STRING),
        # A sidebar-folder reference — a folder id OR a ``/``-separated human
        # path, the same shape ``chat_folder_move_session.folder`` takes — so
        # filing is atomic with creation instead of a create-then-move pair a
        # folder delete can land between (#6118). Bounded like every other
        # folder reference; the two readings share no charset, so only the
        # length is checked here.
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

SESSION_STOP_SCHEMA = ToolSchema(
    tool_name="session_stop",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_CLOSE_SCHEMA = ToolSchema(
    tool_name="session_close",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_SEND_SCHEMA = ToolSchema(
    tool_name="session_send",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("message", str, required=True, max_len=MAX_LONG_STRING),
    ],
)

SESSION_READ_MESSAGE_SCHEMA = ToolSchema(
    tool_name="session_read_message",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("limit", int, required=False, min_val=1, max_val=100, default=20),
        FieldSpec("since", int, required=False, min_val=0),
    ],
)

# ── Schema Registry ──

MCP_CORE_SCHEMAS: dict[str, ToolSchema] = {
    "spawn_run": SPAWN_RUN_SCHEMA,
    "spawn_sub_agents": SPAWN_SUB_AGENTS_SCHEMA,
    "spawn_list": SPAWN_LIST_SCHEMA,
    "resource_status": RESOURCE_STATUS_SCHEMA,
    "spawn_status": SPAWN_STATUS_SCHEMA,
    "learn_add": LEARN_ADD_SCHEMA,
    "learn_remove": LEARN_REMOVE_SCHEMA,
    "session_ledger_read": SESSION_LEDGER_READ_SCHEMA,
    "session_ledger_record": SESSION_LEDGER_RECORD_SCHEMA,
    "skill_search": SKILL_SEARCH_SCHEMA,
    "skill_discover": SKILL_DISCOVER_SCHEMA,
    "skill_fetch": SKILL_FETCH_SCHEMA,
    "task_run": TASK_RUN_SCHEMA,
    "send_message": SEND_MESSAGE_SCHEMA,
    "send_notification": SEND_NOTIFICATION_SCHEMA,
    "read_slack_profile": READ_SLACK_PROFILE_SCHEMA,
    "wait": WAIT_SCHEMA,
    "register_hook": REGISTER_HOOK_SCHEMA,
    "file_send": FILE_SEND_SCHEMA,
    "autonudge_stop": AUTONUDGE_STOP_SCHEMA,
    "monitor_watch": MONITOR_WATCH_SCHEMA,
    "monitor_inspect": MONITOR_INSPECT_SCHEMA,
    "monitor_stop": MONITOR_STOP_SCHEMA,
    "monitor_start": MONITOR_START_SCHEMA,
    "monitor_update": MONITOR_UPDATE_SCHEMA,
    "ask_question": ASK_QUESTION_SCHEMA,
    "delete_message": DELETE_MESSAGE_SCHEMA,
    "local_knowledge_search": LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    "knowledge_dedup": KNOWLEDGE_DEDUP_SCHEMA,
    "knowledge_add_document": KNOWLEDGE_ADD_DOCUMENT_SCHEMA,
    "knowledge_list_sources": KNOWLEDGE_LIST_SOURCES_SCHEMA,
    "search_chat_history": SEARCH_CHAT_HISTORY_SCHEMA,
    "get_chat_session": GET_CHAT_SESSION_SCHEMA,
    "list_sessions": LIST_SESSIONS_SCHEMA,
    "set_project": SET_PROJECT_SCHEMA,
    "reset_conversation": RESET_CONVERSATION_SCHEMA,
    "suggest_followup": SUGGEST_FOLLOWUP_SCHEMA,
    "artifact_save": ARTIFACT_SAVE_SCHEMA,
    "artifact_get": ARTIFACT_GET_SCHEMA,
    "artifact_update": ARTIFACT_UPDATE_SCHEMA,
    "artifact_delete": ARTIFACT_DELETE_SCHEMA,
    "artifact_list": ARTIFACT_LIST_SCHEMA,
    "artifact_versions": ARTIFACT_VERSIONS_SCHEMA,
    "artifact_revert": ARTIFACT_REVERT_SCHEMA,
    "artifact_get_comments": ARTIFACT_GET_COMMENTS_SCHEMA,
    "artifact_post_comment": ARTIFACT_POST_COMMENT_SCHEMA,
    "artifact_reply_comment": ARTIFACT_REPLY_COMMENT_SCHEMA,
    "artifact_mark_review": ARTIFACT_MARK_REVIEW_SCHEMA,
    "artifact_delete_comment": ARTIFACT_DELETE_COMMENT_SCHEMA,
    "artifact_folder_list": ARTIFACT_FOLDER_LIST_SCHEMA,
    "artifact_folder_create": ARTIFACT_FOLDER_CREATE_SCHEMA,
    "artifact_folder_rename": ARTIFACT_FOLDER_RENAME_SCHEMA,
    "artifact_folder_move": ARTIFACT_FOLDER_MOVE_SCHEMA,
    "artifact_folder_delete": ARTIFACT_FOLDER_DELETE_SCHEMA,
    "artifact_move": ARTIFACT_MOVE_SCHEMA,
    # These tools validate their args internally via validate_tool_args(); they
    # MUST be registered here so the outer guard in call_tool_with_logging
    # routes validation through the guarded step (returning a clean "Error:"
    # string). A tool absent from this registry has its args passed through raw,
    # and its internal ValidationError propagates out of the stdio loop, killing
    # the whole kirocrew-core server for the session.
    "workflow_author": WORKFLOW_AUTHOR_SCHEMA,
    "workflow_run": WORKFLOW_RUN_SCHEMA,
    "workflow_status": WORKFLOW_RUN_ID_SCHEMA,
    "workflow_result": WORKFLOW_RUN_ID_SCHEMA,
    "workflow_cancel": WORKFLOW_RUN_ID_SCHEMA,
    "workflow_rerun_subtree": WORKFLOW_RERUN_SCHEMA,
    "workflow_library_list": WORKFLOW_LIBRARY_LIST_SCHEMA,
    "deploy_artifact": DEPLOY_ARTIFACT_SCHEMA,
    "issue_radar_record_investigation": ISSUE_RADAR_RECORD_INVESTIGATION_SCHEMA,
    "ops_mission_control_api": OPS_MISSION_CONTROL_API_SCHEMA,
    # Registered even though ``issue_radar_crew_read`` takes no arguments: an
    # unregistered tool's args pass through raw, and the empty-field schema is
    # also what makes an unknown arg an "Error:" string instead of a stdio-loop
    # crash that takes the whole kirocrew-core server down for the session.
    "issue_radar_crew_read": ISSUE_RADAR_CREW_READ_SCHEMA,
    "issue_radar_crew_record": ISSUE_RADAR_CREW_RECORD_SCHEMA,
}

MCP_CRON_SCHEMAS: dict[str, ToolSchema] = {
    "cron_list": CRON_LIST_SCHEMA,
    "cron_add": CRON_ADD_SCHEMA,
    "cron_update": ToolSchema(
        tool_name="cron_update",
        fields=[
            FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
            FieldSpec("name", str, max_len=MAX_SHORT_STRING),
            FieldSpec("message", str, max_len=MAX_CRON_MESSAGE),
            FieldSpec("cron_expr", str, max_len=100),
            FieldSpec("every", int, min_val=60, max_val=86400 * 30),
            FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=_AGENT_NAME_RE),
            FieldSpec("model", str, max_len=MAX_SHORT_STRING, pattern=_MODEL_NAME_RE),
            FieldSpec("channel", str, max_len=CHANNEL_MAX_LEN, pattern=CHANNEL_ID_RE),
            FieldSpec("thread_ts", str, max_len=30, pattern=re.compile(r"^\d+\.\d+$")),
            FieldSpec("approval_mode", str, max_len=10, pattern=re.compile(r"^(auto)?$")),
            FieldSpec("silent", bool),
            FieldSpec("strict_schedule", bool),
            FieldSpec(
                "skip_dates",
                list,
                item_type=str,
                item_max_len=10,
                max_items=366,
                item_pattern=re.compile(r"^\d{4}-\d{2}-\d{2}$"),
            ),
            FieldSpec("timezone", str, max_len=50, pattern=re.compile(r"^[A-Za-z0-9_/+-]+$")),
            FieldSpec("persistent_session", bool),
            FieldSpec("minimal_context", bool),
            FieldSpec("hide_in_chat", bool),
            FieldSpec("timeout", int, min_val=0, max_val=3600),
            FieldSpec("timeout_secs", int, min_val=1, max_val=86400),
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
    # Agent-initiated vault-secret REQUEST for a script cron. Records a
    # pending grant only; the operator approves in the dashboard. The mapping's
    # keys/values are re-validated at the persistence layer
    # (cron_script.validate_secret_env_grant) — this schema bounds shape/size.
    "cron_secret_request": ToolSchema(
        tool_name="cron_secret_request",
        fields=[
            FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
            FieldSpec("secrets", dict, required=True),
        ],
    ),
}


# ── Tool Schemas (MCP Computer Use — server ``kirocrew-computer``) ──
#
# EVERY computer-use tool MUST have an entry here, including the ones with no
# arguments. Two independent reasons, both load-bearing:
#
#   1. A tool absent from its schema map passes its arguments RAW through the
#      server's ``_validate_args`` (the ``if schema:`` fallback the cron/core
#      servers use), so an unvalidated dict would reach a handler that can
#      synthesize keystrokes into another application's window.
#   2. The dispatcher refuses any tool name not present here, which makes this
#      dict the registration surface: a tenth tool added without a row is
#      rejected rather than silently ungoverned.
#
# Bounds come from ``computer_use.types`` rather than being re-spelled, so the
# schema ceiling and the driver's own budgets cannot drift. ``element_index`` is
# an ``int`` FieldSpec, which ``validate_field`` guards against a bool
# masquerading as an int (``True == 1`` would otherwise pass a range check and
# address element 1).
_CU_SCROLL_DIRECTIONS = frozenset(_cu_types.SCROLL_DIRECTIONS)
# Key specs and accessibility action names are opaque single tokens. Anchored and
# whitespace-free so a spec cannot smuggle a newline into a rendered result, and
# deliberately NOT an enum: ``keymap.parse_key`` owns the key vocabulary (a
# regex enum here would have to be kept in sync with it and would fail closed on
# a legitimate alias), and accessibility action names are app-defined.
_CU_KEY_SPEC_RE = re.compile(r"^[A-Za-z0-9+_.,;:'\"`~!@#$%^&*()\[\]{}<>?/\\|=-]+$")
_CU_AX_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# ``app`` is the agent's REQUEST for a target, matched case-insensitively against
# the on-screen window list. It is never the authorization identity — the gate is
# queried with what the driver RESOLVED — so it only needs to be a bounded,
# single-line string.
_CU_APP_FIELD = FieldSpec("app", str, required=True, max_len=MAX_SHORT_STRING)
_CU_ELEMENT_FIELD = FieldSpec(
    "element_index", int, required=True, min_val=0, max_val=_cu_types.MAX_ELEMENT_INDEX
)
# ``computer_click``'s element index is OPTIONAL because the tool also accepts a
# coordinate target. "Exactly one of (element_index | x+y)" is a CROSS-FIELD rule
# and ``validate_tool_args`` validates fields independently, so it is enforced at
# the dispatch chokepoint by ``policy.check_click_target`` — which the in-process
# entry point also traverses. Keeping it out of the schema is deliberate: a
# ``custom_validator`` here would duplicate the rule in a second place that the
# gateway-side caller could still bypass.
_CU_OPTIONAL_ELEMENT_FIELD = FieldSpec(
    "element_index", int, min_val=0, max_val=_cu_types.MAX_ELEMENT_INDEX
)
_CU_CLICK_METHODS = frozenset(_cu_types.CLICK_METHODS)
_CU_MOUSE_BUTTONS = frozenset(_cu_types.MOUSE_BUTTONS)
_CU_DRAG_PATHS = frozenset(_cu_types.DRAG_PATHS)
# ``computer_launch_app``'s target is a NAME, and this field is where that is
# enforced on the wire. Deliberately the same bounded single-line string shape as
# ``app`` — there is no path field and no argument field, because a launch that
# accepted either would be "run an arbitrary program with attacker-chosen input"
# rather than "open an application". The drivers narrow it much further (a resolved
# executable must sit under a protected install root); this is only the outer bound.
_CU_LAUNCH_APP_FIELD = FieldSpec("app", str, required=True, max_len=_cu_types.MAX_LAUNCH_QUERY_LEN)


def _cu_coord_field(name: str, *, required: bool = False) -> FieldSpec:
    """A screen-coordinate field, bounded so nonsense never reaches the FFI.

    ``(int, float)`` rather than ``float``: JSON has one number type and a model
    writing ``x: 400`` produces an ``int``, which a ``float``-only spec would
    reject for no reason. ``bool`` is still refused — it is an ``int`` subclass, and
    ``validate_field`` rejects it unless ``bool`` is explicitly in the tuple.

    The bound is a fixed generous range rather than a display-derived one: a
    multi-display desktop legitimately extends far past one screen, and the OS
    clamps a delivered event anyway.
    """
    return FieldSpec(
        name,
        (int, float),
        required=required,
        min_val=_cu_types.MIN_SCREEN_COORD,
        max_val=_cu_types.MAX_SCREEN_COORD,
    )


# ── Tool Schemas (MCP Dashboard — server ``kirocrew-dashboard``) ──
#
# Registered separately from MCP_CORE_SCHEMAS because the dashboard-control tools
# ship in their own MCP server: core is the always-present surface, and a
# capability the user opts into does not belong in every session's context. The
# registry must exist for the same reason the core one does — call_tool_with_logging
# routes validation through it, and a tool absent from its server's registry has
# its args passed through raw.
MCP_DASHBOARD_SCHEMAS: dict[str, ToolSchema] = {
    "session_create": SESSION_CREATE_SCHEMA,
    "session_stop": SESSION_STOP_SCHEMA,
    "session_close": SESSION_CLOSE_SCHEMA,
    "session_send": SESSION_SEND_SCHEMA,
    "session_read_message": SESSION_READ_MESSAGE_SCHEMA,
    "chat_folder_tree": CHAT_FOLDER_TREE_SCHEMA,
    "chat_folder_create": CHAT_FOLDER_CREATE_SCHEMA,
    "chat_folder_move": CHAT_FOLDER_MOVE_SCHEMA,
    "chat_folder_move_session": CHAT_FOLDER_MOVE_SESSION_SCHEMA,
}

MCP_COMPUTER_SCHEMAS: dict[str, ToolSchema] = {
    _cu_types.TOOL_LIST_APPS: ToolSchema(tool_name=_cu_types.TOOL_LIST_APPS, fields=[]),
    _cu_types.TOOL_LAUNCH_APP: ToolSchema(
        tool_name=_cu_types.TOOL_LAUNCH_APP,
        # ONE field, and the absence of the others is the point. No ``path``, no
        # ``args``, no ``url``: the target is a name resolved against the OS's own
        # catalog of installed applications, and each of those fields would turn this
        # into a way to run an arbitrary program with attacker-chosen input — which,
        # because computer use is deliberately not governance-gated, would also skip
        # the ``BUILTIN_DENIED_RULES`` floor every ``bash`` call passes.
        fields=[_CU_LAUNCH_APP_FIELD],
    ),
    _cu_types.TOOL_GET_STATE: ToolSchema(
        tool_name=_cu_types.TOOL_GET_STATE,
        fields=[
            _CU_APP_FIELD,
            FieldSpec("text_limit", int, min_val=1, max_val=_cu_types.MAX_TEXT_LIMIT),
            FieldSpec("max_tree_nodes", int, min_val=1, max_val=_cu_types.MAX_TREE_NODES_LIMIT),
            FieldSpec("max_tree_depth", int, min_val=1, max_val=_cu_types.MAX_TREE_DEPTH_LIMIT),
            # No default: absent means "use the operator's config", which is
            # resolved in ``service.snapshot_request``. A default here would
            # override the operator's ``attach_screenshot: false``.
            FieldSpec("screenshot", bool),
        ],
    ),
    _cu_types.TOOL_CLICK: ToolSchema(
        tool_name=_cu_types.TOOL_CLICK,
        fields=[
            _CU_APP_FIELD,
            _CU_OPTIONAL_ELEMENT_FIELD,
            _cu_coord_field("x"),
            _cu_coord_field("y"),
            # No default on ``click_count``: absent means "one click", which the
            # dispatcher supplies from ``DEFAULT_CLICK_COUNT``. Bounded at 3 because
            # macOS itself only reports up to a triple click as a distinct gesture.
            FieldSpec(
                "click_count",
                int,
                min_val=_cu_types.MIN_CLICK_COUNT,
                max_val=_cu_types.MAX_CLICK_COUNT,
            ),
            # Closed enums, not free strings: an unknown value must be refused
            # before any event is synthesized, because a substituted button or
            # method performs a DIFFERENT gesture in a live application.
            FieldSpec("mouse_button", str, max_len=8, allowed=_CU_MOUSE_BUTTONS),
            FieldSpec("click_method", str, max_len=16, allowed=_CU_CLICK_METHODS),
        ],
    ),
    _cu_types.TOOL_DRAG: ToolSchema(
        tool_name=_cu_types.TOOL_DRAG,
        fields=[
            _CU_APP_FIELD,
            # All four coordinates REQUIRED: a drag has no element form (no
            # accessibility action expresses a sweep between two points), so a
            # partial pair has no meaning to fall back on.
            _cu_coord_field("from_x", required=True),
            _cu_coord_field("from_y", required=True),
            _cu_coord_field("to_x", required=True),
            _cu_coord_field("to_y", required=True),
            FieldSpec("mouse_button", str, max_len=8, allowed=_CU_MOUSE_BUTTONS),
            # ``accessibility`` is accepted by the enum but refused by the driver
            # for a drag — enumerating a smaller set here would put the same rule
            # in two places and let them drift.
            FieldSpec("click_method", str, max_len=16, allowed=_CU_CLICK_METHODS),
            # The PATH shape. ``steps`` has no default here: absent means the
            # dispatcher's ``DEFAULT_DRAG_STEPS`` (the two-point form), and putting a
            # default in the schema as well would make one of the two authoritative
            # by accident.
            FieldSpec(
                "steps",
                int,
                min_val=_cu_types.MIN_DRAG_STEPS,
                max_val=_cu_types.MAX_DRAG_STEPS,
            ),
            FieldSpec("path", str, max_len=16, allowed=_CU_DRAG_PATHS),
        ],
    ),
    _cu_types.TOOL_TYPE_TEXT: ToolSchema(
        tool_name=_cu_types.TOOL_TYPE_TEXT,
        fields=[
            _CU_APP_FIELD,
            FieldSpec("text", str, required=True, max_len=_cu_types.MAX_TYPE_TEXT_LEN),
            # REQUIRED, and a security control rather than an ergonomic choice: an
            # unnamed target has no role or subrole, so ``policy.check_input_target``
            # has nothing to inspect and the keystrokes would land in whatever the
            # app happened to focus — possibly a password field. Enforced again at
            # the chokepoint by ``tools._ELEMENT_REQUIRED_TOOLS``; both layers say
            # required so a conforming call is never refused on arrival.
            _CU_ELEMENT_FIELD,
        ],
    ),
    _cu_types.TOOL_PRESS_KEY: ToolSchema(
        tool_name=_cu_types.TOOL_PRESS_KEY,
        fields=[
            _CU_APP_FIELD,
            # REQUIRED for the same reason as ``computer_type_text``, plus one of its
            # own: ``press_key('tab')`` can MOVE focus onto a password box, so the
            # keystroke after an indexless call would land there. The driver focuses
            # the named element first, which is what makes the secure-field refusal
            # meaningful.
            _CU_ELEMENT_FIELD,
            FieldSpec(
                "key",
                str,
                required=True,
                max_len=_cu_types.MAX_KEY_LEN,
                pattern=_CU_KEY_SPEC_RE,
            ),
        ],
    ),
    _cu_types.TOOL_SET_VALUE: ToolSchema(
        tool_name=_cu_types.TOOL_SET_VALUE,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec("value", str, required=True, max_len=_cu_types.MAX_TYPE_TEXT_LEN),
        ],
    ),
    _cu_types.TOOL_SCROLL: ToolSchema(
        tool_name=_cu_types.TOOL_SCROLL,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec("direction", str, required=True, max_len=8, allowed=_CU_SCROLL_DIRECTIONS),
            FieldSpec(
                "pages",
                (int, float),
                min_val=_cu_types.MIN_SCROLL_PAGES,
                max_val=_cu_types.MAX_SCROLL_PAGES,
                default=_cu_types.DEFAULT_SCROLL_PAGES,
            ),
        ],
    ),
    _cu_types.TOOL_PERFORM_ACTION: ToolSchema(
        tool_name=_cu_types.TOOL_PERFORM_ACTION,
        fields=[
            _CU_APP_FIELD,
            _CU_ELEMENT_FIELD,
            FieldSpec(
                "action",
                str,
                required=True,
                max_len=_cu_types.MAX_ACTION_LEN,
                pattern=_CU_AX_ACTION_RE,
            ),
        ],
    ),
    _cu_types.TOOL_END_TURN: ToolSchema(tool_name=_cu_types.TOOL_END_TURN, fields=[]),
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
# Bounds live near ASK_QUESTION_SCHEMA above (single definition, two consumers).


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
    seen_questions: set[str] = set()
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
        seen_labels: set[str] = set()
        for o in raw_opts[:_ASK_MAX_OPTIONS]:
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or "")[:_ASK_MAX_LABEL_LEN]
            if not label:
                continue
            # Option labels are their end-to-end identity: the frontend keys
            # selection state by label and sends labels back as the answer.
            # Descriptions are display-only, so duplicate normalized labels
            # would make distinct-looking rows submit the same value.
            norm_label = " ".join(label.split()).casefold()
            if norm_label in seen_labels:
                raise ValidationError("questions", "duplicate option labels are not allowed")
            seen_labels.add(norm_label)
            desc = str(o.get("description") or "")[:_ASK_MAX_DESC_LEN]
            opts.append({"label": label, "description": desc})
        if not opts:
            continue
        # Answers are keyed by question text end-to-end (the frontend builds an
        # answer map keyed on the question string, and the tool result echoes
        # that map). Two questions with the same text collapse to one entry —
        # the user answers both but only the last reaches the blocked agent.
        # Reject duplicates (normalized on whitespace/case) so a multi-question
        # card can never silently drop an answer.
        norm = " ".join(qt.split()).casefold()
        if norm in seen_questions:
            raise ValidationError("questions", "duplicate question text is not allowed")
        seen_questions.add(norm)
        result.append(
            {
                "question": qt,
                "header": qh,
                "options": opts,
                "multiSelect": bool(q.get("multiSelect")),
            }
        )
    if not result:
        raise ValidationError("questions", "no valid questions after validation")
    return result
