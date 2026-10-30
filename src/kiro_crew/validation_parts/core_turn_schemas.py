"""``kirocrew-core`` tool schemas a turn uses on its own conversation and workspace.

Message edits, the skill catalog, ``kiro_cli_logs``, ``set_project``,
``reset_conversation``, ``chat_tag``, ``suggest_followup`` with its git branch
grammar, ``nothing_to_do`` and the ``workflow_*`` tools.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.constants import MAX_SHORT_STRING, WINDOWS_DEVICE_STEMS
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import (
    MAX_LONG_STRING,
    MAX_MEDIUM_STRING,
    MAX_SKILL_KEY_CHARS,
)
from kiro_crew.validation_parts.text_sanitizer import sanitize_string

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

# update_message addresses a message the same way delete_message does, so it is
# schema-gated for the same reason (a missing key must be a clean ValidationError,
# not a crash out of the stdio loop). ``text``/``blocks`` are optional HERE and
# bounded like SEND_MESSAGE's: the "one of the two is required" rule is the
# handler's, because a schema cannot express the either/or.
UPDATE_MESSAGE_SCHEMA = ToolSchema(
    tool_name="update_message",
    fields=[
        FieldSpec("channel", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("ts", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("text", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec("blocks", list, item_type=dict, max_items=50),
    ],
)

SKILL_SEARCH_SCHEMA = ToolSchema(
    tool_name="skill_search",
    fields=[
        FieldSpec("query", str, max_len=MAX_SHORT_STRING),
        FieldSpec("limit", int),
        FieldSpec("offset", int),
        FieldSpec("action", str, allowed=frozenset({"search", "list", "read"})),
        FieldSpec("key", str, max_len=MAX_SKILL_KEY_CHARS),
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

# kiro_cli_logs reads kiro-cli's own log files (never the fenced identity
# stores) and returns a redacted tail. ``tail`` is a line count bounded by the
# reader's own hard BYTE cap — the schema ceiling only guards against an absurd
# value; the byte cap is what actually bounds the output. ``since`` is a leading
# slice of a log line's own timestamp, matched lexically, so it is a short
# string, not a parsed datetime.
KIRO_CLI_LOGS_SCHEMA = ToolSchema(
    tool_name="kiro_cli_logs",
    fields=[
        FieldSpec("tail", int, min_val=1, max_val=100000),
        FieldSpec("since", str, max_len=MAX_SHORT_STRING),
    ],
)

# Absolute filesystem path: POSIX "/x" and the Windows drive root "C:\x" /
# "C:/x". Root PREFIX only, so a POSIX body may carry ":" and drive-relative
# "C:foo" is refused. Two-backslash roots stay out: "\\host\share" resolves by
# contacting the named host, and "\\?\D:\" carries a prefix the sensitive-path
# fence does not fold -- see the PR. An empty string (clear) skips this check.
_ABSOLUTE_PATH_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")

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


def _validate_chat_tag(cleaned: dict[str, Any]) -> None:
    """At least one of set_state / add / remove must be present.

    An empty call is a no-op the applier would otherwise have to special-case;
    reject it at the boundary so the model gets a clear "nothing to do" signal
    rather than a silent success."""
    if not cleaned.get("set_state") and not cleaned.get("add") and not cleaned.get("remove"):
        raise ValidationError("set_state", "at least one of set_state, add, or remove is required")


# chat_tag lets an agent tag ITS OWN chat slot: set the mutually-exclusive
# workflow state, and/or add/remove non-state tags. Requests carry a tag id
# (a dashboard-minted 12-hex id or a built-in default -- the closed grammar
# context._board_safe_tag_name admits onto the [BOARD] rail) OR a display
# name — names may contain spaces and punctuation —
# and the applier resolves them against the live vocabulary
# case-insensitively, enforcing the per-tag agent policy. The shape gate here
# only bounds the argv (count + per-item length); it deliberately carries NO
# character pattern, because the resolver matches display names verbatim and
# a name like "Needs: review" is valid vocabulary — a charset gate here would
# refuse handles the resolver could match, while admitting nothing an
# attacker needs (authorization lives in the grants store, not the argv).
# request_server names a server and where it comes from; the gateway resolves
# the rest. Bounded here so a malformed call is a clean ValidationError.
REQUEST_SERVER_SCHEMA = ToolSchema(
    tool_name="request_server",
    fields=[
        FieldSpec("server", str, max_len=128),
        FieldSpec("source", str, required=True, max_len=16),
        FieldSpec("registry_id", str, max_len=200),
        FieldSpec("reason", str, max_len=500),
    ],
)

CHAT_TAG_SCHEMA = ToolSchema(
    tool_name="chat_tag",
    fields=[
        FieldSpec("set_state", str, max_len=64),
        FieldSpec(
            "add",
            list,
            item_type=str,
            item_max_len=64,
            max_items=32,
        ),
        FieldSpec(
            "remove",
            list,
            item_type=str,
            item_max_len=64,
            max_items=32,
        ),
    ],
    custom_validator=_validate_chat_tag,
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
    for part in branch.split(sep="/"):
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

NOTHING_TO_DO_SCHEMA = ToolSchema(
    tool_name="nothing_to_do",
    fields=[
        # Clamped, not rejected, for the same reason as ``autonudge_stop``'s
        # ``reason``: the note selects no behavior — the consumer only records
        # it on the quiet transcript step — so a long note must not cost the
        # quiet end itself and fire the consumer's lost-marker WARNING.
        FieldSpec("note", str, max_len=MAX_SHORT_STRING, clamp_to_max=True),
    ],
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
