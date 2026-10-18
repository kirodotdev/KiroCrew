"""The ``kirocrew-cron`` server's tool schemas and its registry, ``MCP_CRON_SCHEMAS``.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import (
    _JOB_ID_RE,
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    MAX_CRON_MESSAGE,
    REGISTERED_AGENT_NAME_RE,
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
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=REGISTERED_AGENT_NAME_RE),
        FieldSpec("member_id", str, max_len=MAX_SHORT_STRING),
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
        FieldSpec("folder", str, max_len=MAX_SHORT_STRING),
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
        # Registered here as well as in the tool's own inputSchema: _validate_args
        # drops any field this list does not name, so a schema-only addition would
        # silently never reach the handler.
        FieldSpec("json", bool),
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


def _validate_cron_secret_request(cleaned: dict[str, Any]) -> None:
    """``secrets`` is required unless the call withdraws the pending request.

    ``withdraw: true`` spells the empty mapping for a client that drops ``{}`` before
    it reaches the server, so it stands in for ``secrets`` and is refused beside a
    mapping that names a grant. A call with neither gets the refusal any missing
    required field gets.
    """
    withdraw = cleaned.get("withdraw") is True
    if withdraw and cleaned.get("secrets"):
        raise ValidationError(
            "withdraw", "withdraws the pending request, so pass it without secrets"
        )
    if cleaned.get("secrets") is None and not withdraw:
        raise ValidationError("secrets", "required")


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
            FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=REGISTERED_AGENT_NAME_RE),
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
            FieldSpec("clear_skip_dates", bool),
            FieldSpec("timezone", str, max_len=50, pattern=re.compile(r"^[A-Za-z0-9_/+-]+$")),
            FieldSpec("folder", str, max_len=MAX_SHORT_STRING),
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
        custom_validator=_validate_cron_secret_request,
        fields=[
            FieldSpec("job_id", str, required=True, max_len=16, pattern=_JOB_ID_RE),
            FieldSpec("secrets", dict),
            FieldSpec("withdraw", bool),
        ],
    ),
}
