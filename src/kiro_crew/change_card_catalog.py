"""The change-card catalog: what an agent may propose, and what each proposal does.

A change card is an agent's proposal of ONE registered kind of change. The agent
names the kind and fills in its parameters; it never supplies a route, a request
body, markup or a risk level. Everything the browser later sends, and everything
the card shows under "what changes", is derived HERE from the parameters and the
gateway's own read of the current state:

* :func:`validate_params` normalizes and bounds the parameters, refusing unknown
  keys, so a card can only carry the fields its kind declares.
* :func:`build_preview` turns parameters plus the current-state snapshot into the
  card's title, product-written ``changes`` rows, the computed ``risk`` and the
  ordered ``apply`` request plan against the EXISTING settings-page routes.
* :func:`build_undo` derives the undo plan after apply, from the state before
  the change and the identities the real routes returned (a created schedule's
  id, a created crewmate's key). Undo restores the state before the change; when
  that is impossible (an overwritten secret) the card says so instead.

Risk is computed from the diff, never from the kind the agent picked: a plain
``setting.change`` that turns on auto-approval is ``widen`` exactly like a trust
card. ``code_exec`` marks a change that runs third-party code on this machine.

A plan step is ``{method, path, body}`` with ``body`` canonical JSON (or ``None``).
A field the gateway cannot know at proposal time is named in the step's ``fill``
list and carries a ``{{...}}`` placeholder in ``body``: ``source: "user"`` is a
value only the person types (a secret's value, never stored anywhere), and
``source: "step"`` is a field a previous step's real response returned, which
the gateway checks against what it recorded. A ``repeat`` step is a read the
browser polls (an OAuth approval) and completes only on the state the route
itself reports.

Pure: no I/O. The dashboard layer (:mod:`kiro_crew.dashboard.change_cards`) reads
the state and calls in here.
"""

from __future__ import annotations

import copy
import json
import re
import shlex
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kiro_crew import guide_catalog
from kiro_crew import member_goals as _goals
from kiro_crew.config.sections import (
    DEFAULT_MODEL,
    MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE,
    MEMBER_APPROVAL_MODES,
    effective_pin_scope,
    normalize_agent_model,
)

KIND_SETTING_CHANGE = "setting.change"
KIND_SCHEDULE_CREATE = "schedule.create"
KIND_SCHEDULE_UPDATE = "schedule.update"
KIND_CREWMATE_CREATE = "crewmate.create"
KIND_CREWMATE_UPDATE = "crewmate.update"
KIND_CREWMATE_CAPABILITIES = "crewmate.capabilities"
KIND_CREWMATE_GOAL = "crewmate.goal"
KIND_CREWMATE_KEY_RESULTS = "crewmate.key_results"
KIND_CREWMATE_CHECK_INS = "crewmate.check_ins"
KIND_TEMPLATE_UPDATE = "template.update"
KIND_MCP_INSTALL = "mcp.install"
KIND_MCP_ADD_CUSTOM = "mcp.add_custom"
KIND_MCP_TOGGLE = "mcp.toggle"
KIND_CONNECTION_CONNECT = "connection.connect"
KIND_SECRET_SAVE = "secret.save"
KIND_TRUST_APP = "trust.app"
KIND_DENIED_COMMAND = "denied_command"
KIND_FOLDER_STEERING = "folder.steering"

#: The steps of a ``crewmate.create`` apply: the crewmate (0), its first goal
#: in its goal record (1), and its optional work schedule (2).
CREWMATE_CREATE_GOAL_STEP = 1
CREWMATE_CREATE_SCHEDULE_STEP = 2

LIST_OP_ADD = "add"
LIST_OP_REMOVE = "remove"
_LIST_OPS = (LIST_OP_ADD, LIST_OP_REMOVE)
_LIST_ITEM_MAX = 200

RISK_NORMAL = "normal"
RISK_TIGHTEN = "tighten"
RISK_WIDEN = "widen"
RISK_CODE_EXEC = "code_exec"

OP_APPLY = "apply"
OP_UNDO = "undo"

#: The crewmate template a proposed crewmate is built from: the one the
#: Crewmates page's New crewmate card builds from (``NewCrewmateDialog.tsx``
#: ``DEFAULT_CREWMATE_TEMPLATE``).
DEFAULT_CREWMATE_TEMPLATE = "kirocrew"

REASON_MAX_CHARS = 500
_NAME_MAX = 128
_TEXT_MAX = 4000
_CREWMATE_NAME_MAX = 24
_CREWMATE_GOAL_MAX = 200
#: A crewmate's goals and key results: the record's own bounds, so a
#: validated card never fails on apply and the record accepts nothing a card
#: could not propose.
_LONG_TERM_GOAL_MAX = _goals.LONG_TERM_MAX_CHARS
_KR_TEXT_MAX = _goals.KR_TEXT_MAX_CHARS
_KR_VALUE_MAX = _goals.KR_VALUE_MAX_CHARS
_PATTERN_MAX = 500
_MAX_CUSTOM_SERVERS = 8
_MAX_LIST_ITEMS = 300
_MAX_CAPABILITY_OPS = 100
_PARAMS_MAX_BYTES = 32 * 1024
#: The folder route's per-entry bound (``chat_folders.MAX_FOLDER_STEERING_DIR_LEN``),
#: so a canonical path the route accepts is one a stored card keeps.
FOLDER_STEERING_DIR_MAX = 4096
#: The ``template.update`` fields that hold a list, replaced whole, and so the ones
#: its ``clear`` may name.
_TEMPLATE_LIST_FIELDS = ("tools", "allowedTools", "skills")

#: The grammars the real routes enforce, so a card is never stricter than the
#: Settings page: a template name (``validation.TEMPLATE_NAME_RE``) and an app
#: grant name (``apps.execution.APP_NAME_RE``). Crewmate names follow
#: ``members.validate_member_name`` (free text, any script); stored-credential
#: names follow ``POST /api/secrets`` (any non-empty text).
_TEMPLATE_NAME_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,61}[A-Za-z0-9]|[A-Za-z0-9])$")
_APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MCP_NAME_RE = re.compile(r"^[@a-zA-Z0-9][@a-zA-Z0-9/_.:-]*$")
_CONFIG_PATH_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_SETTING_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CRON_EXPR_RE = re.compile(r"^[0-9*/,\- A-Za-z?#LW]{1,100}$")

#: Settings whose change can widen what the agent may do without asking. Each
#: entry orders the values from tightest to widest, so a move toward the end is
#: ``widen`` and a move toward the start is ``tighten``.
_SETTING_ORDER: dict[str, tuple[Any, ...]] = {
    "agent.approval_mode": ("interactive", "auto"),
    "agent.sandbox": ("strict", "auto", "off"),
    "agent.yolo_duration": ("30m", "1h", "6h", "12h", "24h", "until_shutdown"),
    "agent.sandbox_allow_no_isolation": (False, True),
    "dashboard.tailscale.enabled": (False, True),
    "dashboard.tailscale.trust_identity": (False, True),
    "dashboard.tailscale.bind_refresh_chains": (True, False),
    # ``login`` lets one session move between every device of the same tailnet
    # login; ``node`` pins it to one device.
    "dashboard.tailscale.pin_scope": ("node", "login"),
    "instances.enabled": (False, True),
    # On, an agent may add documents to the knowledge library
    # (``knowledge_add_document``); off, that tool refuses.
    "knowledge.auto_add_documents": (False, True),
    "skills.approval_required": (True, False),
    "telemetry.beacon_enabled": (False, True),
}

#: Card-editable config paths (``_EDITABLE_CONFIG``) that no value can widen
#: what an agent may do: models, efforts, timeouts, budgets, display and
#: background-work switches. Every editable path is in exactly one of this set,
#: ``_SETTING_ORDER`` or ``_CODE_EXEC_SETTINGS``, so a new path is classified
#: before a card can show it.
_CANNOT_WIDEN_SETTINGS: frozenset[str] = frozenset(
    {
        "agent.chat_turn_timeout_secs",
        "agent.completion_keep",
        "agent.completion_keep_chars",
        "agent.fallback_model",
        "agent.model",
        "agent.provider",
        "agent.reasoning_effort",
        "agent.refusal_fallback_model",
        "agent.role_efforts.background",
        "agent.role_efforts.subagent",
        "agent.role_models.background",
        "agent.role_models.subagent",
        "agent.soft_stop_budget_secs",
        "agent.tool_search",
        "auto_update",
        "computer_use.max_tree_nodes",
        "computer_use.screenshot_max_px",
        "dashboard.crewmate_threads",
        "dashboard.dynamic_dashboard_cards",
        "dashboard.folder_sort",
        "dashboard.mcp_probe_timeout_secs",
        "dashboard.prevent_sleep",
        "dashboard.recent_tint_count",
        # Stored only: the edition's login handler re-checks every flag against
        # its own allowlist before it runs anything.
        "dashboard.sso_login_flags",
        "dashboard.terminal.completion.enabled",
        "dashboard.terminal.reuse_current",
        "dashboard.title_refresh_every_turns",
        "dashboard.update_nudge",
        "dashboard.user_role",
        "dashboard.user_role_other",
        "dashboard.user_technical_level",
        "decisions.bucket",
        "decisions.model_route.complex",
        "decisions.model_route.medium",
        "decisions.model_route.simple",
        "decisions.nudge_wake.llm_model",
        "decisions.nudge_wake.provider",
        "knowledge.auto_ingest_artifacts",
        "knowledge.dedup_every_n_sweeps",
        "knowledge.embed_rate_limit",
        "knowledge.extraction_effort",
        "knowledge.extraction_model",
        "knowledge.extraction_pool_size",
        "knowledge.folder_ingest_chunk_budget",
        "knowledge.import_chunk_budget",
        "knowledge.sweep_chunk_budget",
        "monitoring.prefer_structured_arming",
        "session.autocompact_pct",
        "session.compact_wait_secs",
        "session.pool_size",
        "session.pool_ttl_secs",
        "session.timeout_secs",
        "session_summary.enabled",
        # Retention and shutdown only decide how long the operator's own
        # workspaces live; the default picks among profiles already saved.
        "session_environments.archived_retention_days",
        "session_environments.default_profile",
        "session_environments.stop_on_shutdown",
        # Generated skills still wait for approval unless
        # ``skills.approval_required`` (ordered above) is turned off.
        "skills.auto_create_from_sessions",
        # Local recording only: the write route refuses the enable on a host
        # where it would start sending data out.
        "telemetry.enabled",
    }
)

#: Settings whose VALUE is a command this machine then runs, so changing one is
#: ``code_exec`` the same as installing an MCP server: the diff is a widen/tighten
#: order only if its direction is meaningful, which a free-form command string
#: has none of. ``dashboard.terminal.shell`` is the shell the dashboard terminal
#: launches; an agent that sets it chooses what program runs on every new
#: terminal, so the card says it runs code regardless of the before/after values.
#: ``agent.acp_backend`` picks the harness program every new session runs, and
#: harnesses differ in how they are made to ask before a tool runs
#: (``agent_sdk.backends.ACP_BACKEND_ROUTING``), so no fixed order fits it either.
#: ``session.pool_agent`` names the agent spec warm-pool processes start with, and
#: a session that names no agent claims one (``session_pool._claim_from_pool``),
#: so it picks which tool grants that session runs under. ``agent.kas_bundle``
#: swaps the program the KAS harness is spawned from (the installed kiro-cli or
#: Kiro Crew's own pinned engine bundle, ``acp.harness.kas.KasHarness.resolve_spawn``); a
#: different binary with its own update cadence is a different program, so a
#: bool flip here is code exec, not a widen or tighten.
_CODE_EXEC_SETTINGS: frozenset[str] = frozenset(
    {
        "agent.acp_backend",
        "agent.kas_bundle",
        "dashboard.terminal.shell",
        "session.pool_agent",
        # The Coder deployment and the template a session's workspace is built
        # from decide which machine runs that session's commands, and the
        # gateway proxies credentialed Git requests for it.
        "session_environments",
        "session_environments.coder_url",
        "session_environments.profiles",
    }
)

_PLACEHOLDER = "{{%s}}"


class CardCatalogError(ValueError):
    """A proposal naming an unknown kind or carrying bad parameters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class KindDef:
    id: str
    summary: str
    params_schema: dict[str, Any]
    editable: tuple[str, ...]


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_CRON_FIELDS = {
    "cron_expr": {"type": "string", "description": "5-field cron, e.g. '0 9 * * 1-5'"},
    "timezone": {"type": "string", "description": "IANA timezone, e.g. 'America/New_York'"},
}

#: A one-shot schedule's wall-clock time: an ISO-8601 local date-time with no
#: offset, read in the card's ``timezone`` (the configured zone when omitted).
_AT_FIELD = {
    "type": "string",
    "description": (
        "One-time run: ISO-8601 local date-time, e.g. '2026-10-04T09:00', read in "
        "`timezone`. Use instead of cron_expr."
    ),
}
_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$")

KINDS: dict[str, KindDef] = {
    k.id: k
    for k in (
        KindDef(
            KIND_SETTING_CHANGE,
            "Change one setting. Pass `setting_id` from find_setting (preferred) "
            "or a raw config `path`, and the new `value`. For a setting find_setting "
            "reports as value_type 'string_list', pass `op` ('add' or 'remove') and "
            "one `item` instead of `value`.",
            _obj(
                {
                    "setting_id": _STR,
                    "path": _STR,
                    "value": {},
                    "op": {"type": "string", "enum": [LIST_OP_ADD, LIST_OP_REMOVE]},
                    "item": _STR,
                },
                [],
            ),
            ("value",),
        ),
        KindDef(
            KIND_SCHEDULE_CREATE,
            "Create a scheduled job that sends `message` to an agent. Give exactly "
            "one of `cron_expr` (recurring) or `at` (runs ONCE, e.g. 'remind me "
            "tomorrow at 9am'), plus the `timezone` either is read in.",
            _obj(
                {"name": _STR, "message": _STR, **_CRON_FIELDS, "at": _AT_FIELD},
                ["name", "message"],
            ),
            ("name", "message", "cron_expr", "at", "timezone"),
        ),
        KindDef(
            KIND_SCHEDULE_UPDATE,
            "Change an existing schedule's name, message, cron or timezone.",
            _obj(
                {
                    "id": _STR,
                    "fields": _obj({"name": _STR, "message": _STR, **_CRON_FIELDS}, []),
                },
                ["id", "fields"],
            ),
            ("fields",),
        ),
        KindDef(
            KIND_CREWMATE_CREATE,
            "Create a crewmate that owns a goal, optionally on a daily/cron schedule.",
            _obj(
                {
                    "name": {"type": "string", "maxLength": _CREWMATE_NAME_MAX},
                    "goal": {"type": "string", "maxLength": _CREWMATE_GOAL_MAX},
                    "schedule": _obj(dict(_CRON_FIELDS), ["cron_expr"]),
                },
                ["name", "goal"],
            ),
            ("name", "goal", "schedule"),
        ),
        KindDef(
            KIND_CREWMATE_UPDATE,
            "Change one crewmate's description, display name, model, effort or the "
            "approval mode of its own chat (normal, trust_reads, trust).",
            _obj(
                {
                    "name": _STR,
                    "fields": _obj(
                        {
                            "description": _STR,
                            "display_name": _STR,
                            "model": _STR,
                            "reasoning_effort": _STR,
                            "approval_mode": _STR,
                        },
                        [],
                    ),
                },
                ["name", "fields"],
            ),
            ("fields",),
        ),
        KindDef(
            KIND_CREWMATE_CAPABILITIES,
            "Change one crewmate's tools / MCP servers / skills / autoApprove. `draft` is "
            "{operations:[{section,id,action:set|remove|inherit,value?}], enroll?}.",
            _obj(
                {
                    "member": _STR,
                    "draft": _obj(
                        {"operations": {"type": "array"}, "enroll": _BOOL},
                        ["operations"],
                    ),
                },
                ["member", "draft"],
            ),
            ("draft",),
        ),
        KindDef(
            KIND_CREWMATE_GOAL,
            "Add one goal for a crewmate, or replace one of its goals: what it works "
            "toward. Without `goal_id` the text is added as a new goal; with the id of "
            "one of its goals (goal_read lists them) "
            "it replaces that goal's text. Goals are stored in its goal record, never "
            "in its rules.",
            _obj(
                {
                    "member": _STR,
                    "long_term": {"type": "string", "maxLength": _LONG_TERM_GOAL_MAX},
                    "goal_id": {"type": "string", "maxLength": 9},
                },
                ["member", "long_term"],
            ),
            ("long_term",),
        ),
        KindDef(
            KIND_CREWMATE_KEY_RESULTS,
            "Set one crewmate's key results: readings that show how far it "
            "is from its goals, replacing the current set. `items` is "
            "[{text, target, goal_ids?}], target a number or a word such as 'yes', "
            "goal_ids the ids of the goals that key result serves.",
            _obj(
                {
                    "member": _STR,
                    "items": {
                        "type": "array",
                        "items": _obj(
                            {
                                "text": {"type": "string", "maxLength": _KR_TEXT_MAX},
                                "target": {"type": "string", "maxLength": _KR_VALUE_MAX},
                                "goal_ids": {
                                    "type": "array",
                                    "items": _STR,
                                },
                            },
                            ["text", "target"],
                        ),
                    },
                },
                ["member", "items"],
            ),
            ("items",),
        ),
        KindDef(
            KIND_CREWMATE_CHECK_INS,
            "Turn one crewmate's check-ins on or off and set when they run: daily "
            "(the Daily Goal Reminder), weekly (the Weekly report, on `weekday`, "
            "0 = Sunday) and monthly (the Monthly review, on `month_day`, 1-28), "
            "all at `time` (HH:MM, 24-hour). Give only the fields that change.",
            _obj(
                {
                    "member": _STR,
                    "daily": _BOOL,
                    "weekly": _BOOL,
                    "monthly": _BOOL,
                    "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
                    "month_day": {"type": "integer", "minimum": 1, "maximum": _goals.MAX_MONTH_DAY},
                    "time": {"type": "string", "maxLength": 5},
                },
                ["member"],
            ),
            ("daily", "weekly", "monthly", "weekday", "month_day", "time"),
        ),
        KindDef(
            KIND_TEMPLATE_UPDATE,
            "Change an agent template's tools, allowedTools, skills or model. Affects "
            "every member built from it. A list field is replaced whole; to empty one, "
            "name it in `clear` (e.g. clear: ['allowedTools']) rather than passing [], "
            "which some clients drop before it reaches the server.",
            _obj(
                {
                    "template": _STR,
                    "fields": _obj(
                        {
                            "tools": {"type": "array", "items": _STR},
                            "allowedTools": {"type": "array", "items": _STR},
                            "skills": {"type": "array", "items": _STR},
                            "model": _STR,
                        },
                        [],
                    ),
                    "clear": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(_TEMPLATE_LIST_FIELDS)},
                    },
                },
                ["template"],
            ),
            ("fields",),
        ),
        KindDef(
            KIND_MCP_INSTALL,
            "Install an MCP server from a discovery provider (runs third-party code).",
            _obj({"provider": _STR, "id": _STR}, ["provider", "id"]),
            (),
        ),
        KindDef(
            KIND_MCP_ADD_CUSTOM,
            "Add custom MCP server(s): {servers:{name:{command,args?,env?}|{url}}, enable}.",
            _obj({"servers": {"type": "object"}, "enable": _BOOL}, ["servers"]),
            ("enable",),
        ),
        KindDef(
            KIND_MCP_TOGGLE,
            "Enable/disable an MCP server ({name, enabled}) or one tool ({server, tool, enabled}).",
            _obj({"name": _STR, "server": _STR, "tool": _STR, "enabled": _BOOL}, ["enabled"]),
            ("enabled",),
        ),
        KindDef(
            KIND_CONNECTION_CONNECT,
            "Connect an account (OAuth) from the Connections registry by slug.",
            _obj({"slug": _STR}, ["slug"]),
            (),
        ),
        KindDef(
            KIND_SECRET_SAVE,
            "Ask the user to store a secret under `name`. The user types the value into the "
            "card; you never see or send it.",
            _obj({"name": _STR}, ["name"]),
            (),
        ),
        KindDef(
            KIND_TRUST_APP,
            "Allow a third-party app's code to run (widens permissions).",
            _obj({"name": _STR, "repository": _STR}, ["name"]),
            (),
        ),
        KindDef(
            KIND_DENIED_COMMAND,
            "Add a user denied-command rule ({action:'add', pattern, note?}) or toggle one "
            "({action:'toggle', id, enabled}).",
            _obj(
                {
                    "action": {"type": "string", "enum": ["add", "toggle"]},
                    "pattern": _STR,
                    "note": _STR,
                    "id": _STR,
                    "enabled": _BOOL,
                },
                ["action"],
            ),
            ("note", "enabled"),
        ),
        KindDef(
            KIND_FOLDER_STEERING,
            "Replace a sidebar folder's additional steering directories (Folder settings "
            "-> Additional steering). `folder` is a folder id from chat_folder_tree; "
            "`steering_dirs` is the COMPLETE new list of absolute directory paths. To "
            "empty it, pass `clear: true` with no steering_dirs ([] means the same, but "
            "some clients drop it before it reaches the server).",
            _obj(
                {
                    "folder": _STR,
                    "steering_dirs": {"type": "array", "items": _STR},
                    "clear": _BOOL,
                },
                ["folder"],
            ),
            ("steering_dirs",),
        ),
    )
}


def list_kinds() -> list[dict[str, Any]]:
    """The catalog as the ``list_change_kinds`` tool returns it."""
    return [
        {
            "id": k.id,
            "summary": k.summary,
            "params_schema": copy.deepcopy(k.params_schema),
            "editable": list(k.editable),
        }
        for k in KINDS.values()
    ]


# ── parameter validation ──


def _fail(code: str, message: str) -> CardCatalogError:
    return CardCatalogError(code, message)


def _clean_str(
    params: dict[str, Any],
    key: str,
    *,
    required: bool = True,
    max_len: int = _NAME_MAX,
    pattern: re.Pattern[str] | None = None,
    allow_empty: bool = False,
) -> str | None:
    value = params.get(key)
    if value is None:
        if required:
            raise _fail("missing_param", f"'{key}' is required")
        return None
    if not isinstance(value, str):
        raise _fail("invalid_param", f"'{key}' must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise _fail("invalid_param", f"'{key}' must not be empty")
    if len(value) > max_len:
        raise _fail("invalid_param", f"'{key}' must be at most {max_len} characters")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value):
        raise _fail("invalid_param", f"'{key}' has control characters")
    if pattern is not None and value and not pattern.fullmatch(value):
        raise _fail("invalid_param", f"'{key}' has invalid characters")
    return value


def _goal_text(params: dict[str, Any], key: str, *, max_len: int, multiline: bool) -> str:
    """*key* as the goal record stores it (:func:`kiro_crew.member_goals.clean_text`).

    A card's params are what its apply writes and what its after-read is compared
    with, so they carry the record's own normalisation: a tab, a line break in a
    one-line field or a format character reads as the store will keep it.
    """
    from kiro_crew import member_goals

    raw = _clean_str(params, key, max_len=max_len)
    try:
        value = member_goals.clean_text(raw, max_chars=max_len, multiline=multiline, what=key)
    except member_goals.GoalError as exc:
        raise _fail("invalid_param", f"'{key}': {exc}") from None
    if not value:
        raise _fail("invalid_param", f"'{key}' must not be empty")
    return value


def _member_name(params: dict[str, Any], key: str, *, max_len: int = _NAME_MAX) -> str:
    """A crewmate name exactly as ``POST /api/agents`` accepts it (any script)."""
    from kiro_crew.members import MemberNameError, validate_member_name

    value = _clean_str(params, key, max_len=max_len)
    assert value is not None
    try:
        return validate_member_name(value)
    except MemberNameError as exc:
        raise _fail("invalid_param", f"'{key}': {exc}") from None


def _clean_bool(params: dict[str, Any], key: str, *, required: bool = True) -> bool | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, bool):
        raise _fail("invalid_param", f"'{key}' must be a boolean")
    return value


def _only(params: dict[str, Any], allowed: set[str], where: str = "params") -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise _fail("unknown_param", f"unknown field '{unknown[0][:64]}' in {where}")


def _clean_cron(params: dict[str, Any], *, required: bool) -> tuple[str | None, str | None]:
    expr = _clean_str(params, "cron_expr", required=required, max_len=100, pattern=_CRON_EXPR_RE)
    if expr is not None and len(expr.split()) != 5:
        raise _fail("invalid_cron", "'cron_expr' must have 5 fields")
    tz = _clean_str(params, "timezone", required=False, max_len=64, allow_empty=True)
    return expr, (tz or None)


def _clean_at(params: dict[str, Any]) -> str:
    """A one-shot ``at``: a real local date-time, normalized, with no offset.

    An offset is refused rather than honoured so the card has ONE zone, the
    ``timezone`` field the user sees and can edit; ``2026-10-04T09:00-04:00``
    beside ``timezone: America/Los_Angeles`` would otherwise mean two instants.
    """
    value = _clean_str(params, "at", max_len=32, pattern=_AT_RE)
    assert value is not None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _fail("invalid_at", "'at' is not a real date and time") from None
    return parsed.isoformat(timespec="minutes" if parsed.second == 0 else "seconds")


def _str_list(value: Any, key: str, *, max_len: int = 512) -> list[str]:
    if not isinstance(value, list) or len(value) > _MAX_LIST_ITEMS:
        raise _fail("invalid_param", f"'{key}' must be a list of at most {_MAX_LIST_ITEMS}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > max_len:
            raise _fail("invalid_param", f"'{key}' items must be non-empty strings")
        if item not in out:
            out.append(item)
    return out


def _template_clear(params: dict[str, Any]) -> tuple[str, ...]:
    """The ``template.update`` list fields a card empties, named in ``clear``.

    A list of names rather than ``fields: {"allowedTools": []}``, because a client
    that drops empty containers delivers that request with the key missing.
    """
    if "clear" not in params:
        return ()
    names = params["clear"]
    if not isinstance(names, list) or not all(
        isinstance(n, str) and n in _TEMPLATE_LIST_FIELDS for n in names
    ):
        raise _fail(
            "invalid_param",
            "'clear' must list template fields to empty: " + ", ".join(_TEMPLATE_LIST_FIELDS),
        )
    return tuple(dict.fromkeys(names))


def _check_mcp_spec_shapes(name: str, spec: dict[str, Any]) -> None:
    """Refuse a custom MCP spec whose launch fields have the wrong type.

    The preview builds a launch line from these and the apply step sends them
    verbatim, so a wrong shape is a bad request (400), never a crash (500).
    """
    for key in ("command", "url"):
        if key in spec and not isinstance(spec[key], str):
            raise _fail("invalid_param", f"server '{name}' {key} must be a string")
    if "args" in spec:
        args = spec["args"]
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise _fail("invalid_param", f"server '{name}' args must be a list of strings")
    for key in ("env", "headers"):
        if key in spec:
            values = spec[key]
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise _fail("invalid_param", f"server '{name}' {key} must map strings to strings")


def validate_params(kind: str, params: Any) -> dict[str, Any]:
    """Normalize *params* for *kind*. Raises :class:`CardCatalogError`."""
    if kind not in KINDS:
        raise _fail("unknown_kind", f"unknown card kind '{str(kind)[:64]}'")
    if not isinstance(params, dict):
        raise _fail("invalid_params", "params must be an object")
    try:
        size = len(json.dumps(params, ensure_ascii=False))
    except (TypeError, ValueError):
        raise _fail("invalid_params", "params must be JSON") from None
    if size > _PARAMS_MAX_BYTES:
        raise _fail("invalid_params", "params are too large")
    p = params
    out: dict[str, Any]
    f: dict[str, Any]
    if kind == KIND_SETTING_CHANGE:
        _only(p, {"setting_id", "path", "value", "op", "item"})
        if ("setting_id" in p) == ("path" in p):
            raise _fail("invalid_param", "give exactly one of 'setting_id' or 'path'")
        if "setting_id" in p:
            out = {"setting_id": _clean_str(p, "setting_id", max_len=200, pattern=_SETTING_ID_RE)}
        else:
            out = {"path": _clean_str(p, "path", max_len=200, pattern=_CONFIG_PATH_RE)}
        if "op" in p or "item" in p:
            # List membership: one item added to or removed from a string list.
            if "value" in p:
                raise _fail("invalid_param", "give either 'value' or 'op' with 'item', not both")
            if p.get("op") not in _LIST_OPS:
                raise _fail("invalid_param", "'op' must be 'add' or 'remove'")
            if not isinstance(p.get("item"), str):
                raise _fail("invalid_param", "'item' must be a string")
            out["op"] = p["op"]
            out["item"] = _clean_str(p, "item", max_len=_LIST_ITEM_MAX)
            return out
        if "value" not in p:
            raise _fail("missing_param", "'value' is required")
        if isinstance(p["value"], list):
            raise _fail("invalid_param", "'value' must be a scalar or a record")
        out["value"] = copy.deepcopy(p["value"])
        return out
    if kind == KIND_SCHEDULE_CREATE:
        _only(p, {"name", "message", "cron_expr", "at", "timezone"})
        if ("cron_expr" in p) == ("at" in p):
            raise _fail("invalid_param", "give exactly one of 'cron_expr' or 'at'")
        out = {
            "name": _clean_str(p, "name"),
            "message": _clean_str(p, "message", max_len=_TEXT_MAX),
        }
        if "at" in p:
            out["at"] = _clean_at(p)
            tz = _clean_str(p, "timezone", required=False, max_len=64, allow_empty=True)
        else:
            out["cron_expr"], tz = _clean_cron(p, required=True)
        if tz:
            out["timezone"] = tz
        return out
    if kind == KIND_SCHEDULE_UPDATE:
        _only(p, {"id", "fields"})
        job_id = _clean_str(p, "id", pattern=_ID_RE)
        fields = p.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise _fail("invalid_param", "'fields' must be a non-empty object")
        _only(fields, {"name", "message", "cron_expr", "timezone"}, "fields")
        f = {}
        if "name" in fields:
            f["name"] = _clean_str(fields, "name")
        if "message" in fields:
            f["message"] = _clean_str(fields, "message", max_len=_TEXT_MAX)
        if "cron_expr" in fields:
            f["cron_expr"] = _clean_cron(fields, required=True)[0]
        if "timezone" in fields:
            f["timezone"] = _clean_str(fields, "timezone", max_len=64, allow_empty=True) or ""
        return {"id": job_id, "fields": f}
    if kind == KIND_CREWMATE_CREATE:
        _only(p, {"name", "goal", "schedule"})
        out = {
            "name": _member_name(p, "name", max_len=_CREWMATE_NAME_MAX),
            "goal": _clean_str(p, "goal", max_len=_CREWMATE_GOAL_MAX),
        }
        schedule = p.get("schedule")
        if schedule is not None:
            if not isinstance(schedule, dict):
                raise _fail("invalid_param", "'schedule' must be an object")
            _only(schedule, {"cron_expr", "timezone"}, "schedule")
            expr, tz = _clean_cron(schedule, required=True)
            out["schedule"] = {"cron_expr": expr, **({"timezone": tz} if tz else {})}
        return out
    if kind == KIND_CREWMATE_UPDATE:
        _only(p, {"name", "fields"})
        name = _member_name(p, "name")
        fields = p.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise _fail("invalid_param", "'fields' must be a non-empty object")
        _only(
            fields,
            {"description", "display_name", "model", "reasoning_effort", "approval_mode"},
            "fields",
        )
        f = {}
        for key in fields:
            f[key] = _clean_str(
                fields, key, max_len=_TEXT_MAX if key == "description" else 64, allow_empty=True
            )
        if "approval_mode" in f and f["approval_mode"] not in MEMBER_APPROVAL_MODES:
            raise _fail(
                "invalid_param",
                "'approval_mode' must be one of " + ", ".join(MEMBER_APPROVAL_MODES),
            )
        return {"name": name, "fields": f}
    if kind == KIND_CREWMATE_CAPABILITIES:
        _only(p, {"member", "draft"})
        member = _member_name(p, "member")
        draft = p.get("draft")
        if not isinstance(draft, dict):
            raise _fail("invalid_param", "'draft' must be an object")
        _only(draft, {"operations", "enroll"}, "draft")
        ops = draft.get("operations")
        if not isinstance(ops, list) or not ops or len(ops) > _MAX_CAPABILITY_OPS:
            raise _fail("invalid_param", "'draft.operations' must be a non-empty list")
        clean_ops = []
        for op in ops:
            if not isinstance(op, dict):
                raise _fail("invalid_param", "each operation must be an object")
            _only(op, {"section", "id", "action", "value"}, "operation")
            if op.get("action") not in ("set", "remove", "inherit"):
                raise _fail("invalid_param", "operation action must be set, remove or inherit")
            if not isinstance(op.get("section"), str) or not isinstance(op.get("id"), str):
                raise _fail("invalid_param", "operation needs section and id")
            if op["action"] == "set" and "value" not in op:
                raise _fail("invalid_param", "a set operation needs a value")
            clean_ops.append(copy.deepcopy(op))
        clean_draft: dict[str, Any] = {"operations": clean_ops}
        if "enroll" in draft:
            clean_draft["enroll"] = _clean_bool(draft, "enroll")
        return {"member": member, "draft": clean_draft}
    if kind == KIND_CREWMATE_GOAL:
        _only(p, {"member", "long_term", "goal_id"})
        out_goal: dict[str, Any] = {
            "member": _member_name(p, "member"),
            "long_term": _goal_text(p, "long_term", max_len=_LONG_TERM_GOAL_MAX, multiline=True),
        }
        if "goal_id" in p:
            if not _goals.is_goal_id(p["goal_id"]):
                raise _fail("invalid_param", "'goal_id' must be one of the crewmate's goal ids")
            out_goal["goal_id"] = p["goal_id"]
        return out_goal
    if kind == KIND_CREWMATE_KEY_RESULTS:
        _only(p, {"member", "items"})
        raw = p.get("items")
        if not isinstance(raw, list) or not raw:
            raise _fail("invalid_param", "'items' must hold at least one key result")
        items: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise _fail("invalid_param", "each key result is {text, target, goal_ids?}")
            _only(item, {"text", "target", "goal_ids"}, "key result")
            kr: dict[str, Any] = {
                "text": _goal_text(item, "text", max_len=_KR_TEXT_MAX, multiline=False),
                "target": _goal_text(item, "target", max_len=_KR_VALUE_MAX, multiline=False),
            }
            if any((k["text"], k["target"]) == (kr["text"], kr["target"]) for k in items):
                raise _fail("invalid_param", "two key results have the same text and target")
            if "goal_ids" in item:
                links = item["goal_ids"]
                if not isinstance(links, list) or not all(_goals.is_goal_id(g) for g in links):
                    raise _fail("invalid_param", "'goal_ids' is a list of the crewmate's goal ids")
                kr["goal_ids"] = list(dict.fromkeys(links))
            items.append(kr)
        return {"member": _member_name(p, "member"), "items": items}
    if kind == KIND_CREWMATE_CHECK_INS:
        _only(p, {"member", *CHECK_IN_FIELDS})
        out_ci: dict[str, Any] = {"member": _member_name(p, "member")}
        for key in ("daily", "weekly", "monthly"):
            if key in p:
                out_ci[key] = _clean_bool(p, key)
        for key, low, high in (("weekday", 0, 6), ("month_day", 1, _goals.MAX_MONTH_DAY)):
            if key in p:
                v = p[key]
                if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
                    raise _fail("invalid_param", f"'{key}' must be {low} to {high}")
                out_ci[key] = v
        if "time" in p:
            t = p["time"]
            if not isinstance(t, str) or not _goals.is_check_in_time(t):
                raise _fail("invalid_param", "'time' must be HH:MM (24-hour)")
            out_ci["time"] = t
        if len(out_ci) == 1:
            raise _fail("invalid_param", "give at least one check-in or its timing")
        return out_ci
    if kind == KIND_TEMPLATE_UPDATE:
        _only(p, {"template", "fields", "clear"})
        template = _clean_str(p, "template", pattern=_TEMPLATE_NAME_RE)
        clear = _template_clear(p)
        fields = p.get("fields")
        if fields is None and clear:
            # ``clear`` is a whole request on its own: a client that drops empty
            # containers delivers no ``fields`` when it held only ``[]`` values.
            fields = {}
        if not isinstance(fields, dict) or not (fields or clear):
            raise _fail(
                "invalid_param",
                "'fields' must be a non-empty object; to empty a list field, name it in 'clear'",
            )
        _only(fields, {*_TEMPLATE_LIST_FIELDS, "model"}, "fields")
        f = {}
        for key in _TEMPLATE_LIST_FIELDS:
            if key in fields:
                f[key] = _str_list(fields[key], key)
            if key in clear:
                if f.get(key):
                    raise _fail(
                        "invalid_param", f"'{key}' is named in 'clear' and given items in 'fields'"
                    )
                f[key] = []
        if "model" in fields:
            f["model"] = _clean_str(fields, "model", max_len=64, allow_empty=True)
        # Normalized to the plain replacement, so the preview, the apply step, the
        # person's edit and Undo read one shape whichever spelling the agent used.
        return {"template": template, "fields": f}
    if kind == KIND_MCP_INSTALL:
        _only(p, {"provider", "id"})
        return {
            "provider": _clean_str(p, "provider", max_len=32, pattern=_SLUG_RE),
            "id": _clean_str(p, "id", max_len=256, pattern=_MCP_NAME_RE),
        }
    if kind == KIND_MCP_ADD_CUSTOM:
        _only(p, {"servers", "enable"})
        servers = p.get("servers")
        if not isinstance(servers, dict) or not servers or len(servers) > _MAX_CUSTOM_SERVERS:
            raise _fail("invalid_param", "'servers' must be a non-empty object of servers")
        clean: dict[str, Any] = {}
        for name, spec in servers.items():
            if not isinstance(name, str) or not valid_mcp_name(name):
                raise _fail("invalid_param", "invalid MCP server name")
            if not isinstance(spec, dict) or not (
                isinstance(spec.get("command"), str) or isinstance(spec.get("url"), str)
            ):
                raise _fail("invalid_param", f"server '{name}' needs a command or a url")
            _check_mcp_spec_shapes(name, spec)
            clean[name] = copy.deepcopy(spec)
        enable = p.get("enable", True)
        if not isinstance(enable, bool):
            raise _fail("invalid_param", "'enable' must be a boolean")
        return {"servers": clean, "enable": enable}
    if kind == KIND_MCP_TOGGLE:
        _only(p, {"name", "server", "tool", "enabled"})
        enabled = _clean_bool(p, "enabled")
        if "tool" in p or "server" in p:
            if "name" in p:
                raise _fail("invalid_param", "give either name, or server and tool")
            return {
                "server": _clean_str(p, "server", pattern=_MCP_NAME_RE),
                "tool": _clean_str(p, "tool", max_len=256, pattern=_MCP_NAME_RE),
                "enabled": enabled,
            }
        return {"name": _clean_str(p, "name", pattern=_MCP_NAME_RE), "enabled": enabled}
    if kind == KIND_CONNECTION_CONNECT:
        _only(p, {"slug"})
        return {"slug": _clean_str(p, "slug", max_len=64, pattern=_SLUG_RE)}
    if kind == KIND_SECRET_SAVE:
        # No ``value`` field exists: a secret's value is typed by the person into
        # the card and sent by the browser straight to the vault route.
        _only(p, {"name"})
        return {"name": _clean_str(p, "name", max_len=_NAME_MAX)}
    if kind == KIND_TRUST_APP:
        _only(p, {"name", "repository"})
        out = {"name": _clean_str(p, "name", max_len=64, pattern=_APP_NAME_RE)}
        repo = _clean_str(p, "repository", required=False, max_len=512)
        if repo:
            out["repository"] = repo
        return out
    if kind == KIND_FOLDER_STEERING:
        _only(p, {"folder", "steering_dirs", "clear"})
        folder = _clean_str(p, "folder", pattern=_ID_RE)
        if _clean_bool(p, "clear", required=False):
            # The spelling of ``steering_dirs: []`` that survives a client which drops
            # an empty list, normalized to that list so every later step reads one shape.
            if p.get("steering_dirs"):
                raise _fail(
                    "invalid_param", "'clear' empties the list, so give it without 'steering_dirs'"
                )
            return {"folder": folder, "steering_dirs": []}
        if "steering_dirs" not in p:
            raise _fail(
                "invalid_param",
                "'steering_dirs' is required; to empty the list, pass 'clear': true",
            )
        return {
            "folder": folder,
            "steering_dirs": _str_list(
                p.get("steering_dirs"), "steering_dirs", max_len=FOLDER_STEERING_DIR_MAX
            ),
        }
    # KIND_DENIED_COMMAND
    action = p.get("action")
    if action == "add":
        _only(p, {"action", "pattern", "note"})
        pattern = _clean_str(p, "pattern", max_len=_PATTERN_MAX)
        try:
            re.compile(pattern or "")
        except re.error:
            raise _fail("invalid_param", "'pattern' is not a valid regex") from None
        note = _clean_str(p, "note", required=False, max_len=200, allow_empty=True) or ""
        return {"action": "add", "pattern": pattern, "note": note}
    if action == "toggle":
        _only(p, {"action", "id", "enabled"})
        return {
            "action": "toggle",
            "id": _clean_str(p, "id", pattern=_ID_RE),
            "enabled": _clean_bool(p, "enabled"),
        }
    raise _fail("invalid_param", "'action' must be 'add' or 'toggle'")


def valid_mcp_name(name: str) -> bool:
    return (
        bool(name)
        and len(name) <= _NAME_MAX
        and ".." not in name
        and bool(_MCP_NAME_RE.fullmatch(name))
    )


def check_editable(kind: str, old: dict[str, Any], new: dict[str, Any]) -> None:
    """Refuse a re-preview that changes a field the kind does not mark editable."""
    editable = set(KINDS[kind].editable)
    for key in set(old) | set(new):
        if key not in editable and old.get(key) != new.get(key):
            raise _fail("field_not_editable", f"'{key}' cannot be changed on this card")


# ── plans ──


def _q(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")


def step(
    method: str,
    path: str,
    body: Any = None,
    *,
    fill: list[dict[str, Any]] | None = None,
    repeat: bool = False,
) -> dict[str, Any]:
    s: dict[str, Any] = {"method": method, "path": path, "body": body}
    if fill:
        s["fill"] = fill
    if repeat:
        s["repeat"] = True
    return s


def _user_fill(field: str) -> dict[str, Any]:
    return {"field": field, "source": "user"}


def _step_fill(field: str, step_index: int, key: str) -> dict[str, Any]:
    return {"field": field, "source": "step", "step": step_index, "key": key}


def _step_path_fill(field: str, step_index: int, key: str) -> dict[str, Any]:
    """A ``step`` fill of one path segment instead of a body field.

    The step's path carries :func:`placeholder` where the segment goes, and the
    value must be a plain identifier (:data:`PATH_FILL_VALUE`), so an earlier
    response can never add a segment or a query to the path.
    """
    return {**_step_fill(field, step_index, key), "in": "path"}


def is_path_fill(fill: dict[str, Any]) -> bool:
    return fill.get("in") == "path"


#: What a path fill may stand for: one segment of identifier characters.
PATH_FILL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


def placeholder(fill: dict[str, Any]) -> str:
    if fill["source"] == "user":
        return _PLACEHOLDER % f"user:{fill['field']}"
    return _PLACEHOLDER % f"step{fill['step']}.{fill['key']}"


def _with_fills(body: dict[str, Any], fills: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(body)
    for f in fills:
        out[f["field"]] = placeholder(f)
    return out


def _cron_body(fields: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key in ("name", "message", "timezone"):
        if key in fields:
            body[key] = fields[key]
    if "cron_expr" in fields:
        body["cron"] = fields["cron_expr"]
    return body


def _list_diff(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    return [x for x in after if x not in before], [x for x in before if x not in after]


def _order_risk(order: tuple[Any, ...], before: Any, after: Any) -> str:
    """Rank a move along *order* (tightest first): toward the end widens."""
    if before not in order or after not in order:
        return RISK_NORMAL
    b, a = order.index(before), order.index(after)
    if a > b:
        return RISK_WIDEN
    if a < b:
        return RISK_TIGHTEN
    return RISK_NORMAL


def _setting_risk(path: str, before: Any, after: Any) -> str:
    if path in _CODE_EXEC_SETTINGS:
        return RISK_CODE_EXEC
    order = _SETTING_ORDER.get(path)
    if path == "dashboard.tailscale.pin_scope":
        # The route takes any short string and the loader applies its
        # normalised form, so rank the values the gateway will apply.
        before, after = effective_pin_scope(before), effective_pin_scope(after)
    if order is not None and before in order and after in order:
        return _order_risk(order, before, after)
    segments = set(guide_catalog._SEGMENT_SPLIT.split(path.lower()))
    if segments & guide_catalog._EXCLUDED_SETTING_SEGMENTS:
        return RISK_WIDEN
    return RISK_NORMAL


STORE_KIROCREW = "kirocrew"
STORE_DASHBOARD = "dashboard"

#: Settings whose Settings-page control writes the DASHBOARD config
#: (``PUT /api/dashboard/config``, partial body) rather than a config path: each
#: registry id maps to the body key ``ChatPanel.tsx`` / ``BrowserPanel.tsx`` send
#: and the values that control offers. A registry id absent here, with no
#: editable ``configKey`` either, has no write path a card may use.
_BOOL_VALUES: tuple[Any, ...] = (True, False)
DASHBOARD_SETTINGS: dict[str, tuple[str, tuple[Any, ...]]] = {
    "chat.response-verbosity": ("verbosity", ("default", "concise", "ultra", "answer_only")),
    "chat.widget-density": ("widget_density", ("more", "less")),
    "chat.quick-send": ("quick_send", _BOOL_VALUES),
    "chat.merge-queued-messages": ("merge_queued_messages", _BOOL_VALUES),
    "chat.link-previews": ("link_previews", _BOOL_VALUES),
    "chat.mcp-apps-in-side-panel": ("mcp_app_panel", _BOOL_VALUES),
    "chat.auto-open-git-in-side-panel": ("auto_open_git_panel", _BOOL_VALUES),
    "chat.split-view-session-grid": ("session_grid", _BOOL_VALUES),
    "chat.tail-only-fork": ("tail_fork_enabled", _BOOL_VALUES),
    "chat.restore-sessions": ("restore_sessions", _BOOL_VALUES),
    "chat.restore-window": ("restore_window_minutes", (15, 30, 60, 120, 360, 720, 1440, 0)),
    "chat.pr-and-issue-chips-on-session-cards": ("session_card_source_links", _BOOL_VALUES),
    "chat.folder-suggestions": ("folder_suggestions_enabled", _BOOL_VALUES),
    "chat.default-memory-mode": ("default_memory_mode", ()),
    "browser.use-the-built-in-browser": ("use_builtin_browser", _BOOL_VALUES),
}
#: Body keys of ``PUT /api/dashboard/config`` a card may write, by key.
DASHBOARD_KEYS = {key: values for key, values in DASHBOARD_SETTINGS.values()}

#: Dashboard settings whose value is a STRING LIST the Settings page edits one
#: item at a time: registry id -> (stored key, add body key, remove body key),
#: exactly the delta keys ``ChatPanel.tsx``'s Selectable Models control sends to
#: ``PUT /api/dashboard/config``. A ``setting.change`` card on one of these takes
#: ``op``/``item`` instead of ``value``.
DASHBOARD_LIST_SETTINGS: dict[str, tuple[str, str, str]] = {
    "chat.selectable-models": (
        "model_picker_hidden_models",
        "model_picker_hidden_models_add",
        "model_picker_hidden_models_remove",
    ),
}
#: The same, by stored key.
DASHBOARD_LIST_KEYS = {v[0]: v for v in DASHBOARD_LIST_SETTINGS.values()}
#: How a one-item edit of a list setting reads, by stored key: (row label, add
#: title, remove title). The registry label names the Settings control, which
#: can say the opposite of the stored list ("Selectable Models" edits the HIDDEN
#: list), so the card names the list it really edits and says what the person
#: will see. A list without an entry reads "Add X to <label>" / "Remove X from".
LIST_SETTING_WORDING: dict[str, tuple[str, str, str]] = {
    "model_picker_hidden_models": (
        "Hidden models",
        "Hide “{item}” from the model picker",
        "Show “{item}” in the model picker",
    ),
}
#: Delta body keys of those lists -> the stored key they edit.
DASHBOARD_LIST_OP_KEYS = {
    op_key: v[0] for v in DASHBOARD_LIST_SETTINGS.values() for op_key in (v[1], v[2])
}


def value_allowed(value: Any, allowed: tuple[Any, ...] | list[Any]) -> bool:
    """Membership that does not let ``1`` stand for ``True`` (or the reverse)."""
    return any(type(value) is type(a) and value == a for a in allowed)


def _setting_risk_key(p: dict[str, Any], snapshot: dict[str, Any]) -> str:
    key = str(snapshot.get("key") or p.get("path") or "")
    if snapshot.get("store") == STORE_DASHBOARD:
        return f"dashboard.{key}"
    return key


def string_items(value: Any) -> list[str]:
    """*value*'s string entries, in order; anything else reads as an empty list."""
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def list_after(before: Any, op: str, item: str) -> list[str]:
    """The string list *before* with *item* added (appended) or removed."""
    items = string_items(before)
    if op == LIST_OP_ADD:
        return items if item in items else [*items, item]
    return [x for x in items if x != item]


def list_op_step(target: dict[str, Any], op: str, item: str) -> dict[str, Any]:
    """The Settings page's own one-item delta request for a string-list setting."""
    _key, add_key, remove_key = DASHBOARD_LIST_KEYS[str(target.get("key"))]
    return step(
        "PUT", "/api/dashboard/config", {add_key if op == LIST_OP_ADD else remove_key: [item]}
    )


def setting_write_step(p: dict[str, Any], target: dict[str, Any], value: Any) -> dict[str, Any]:
    """The one existing request that writes *value* to the setting's own store."""
    key = target.get("key") or p.get("path")
    if target.get("store") == STORE_DASHBOARD:
        return step("PUT", "/api/dashboard/config", {key: value})
    return step("PATCH", "/api/config/kirocrew", {"path": key, "value": value})


def _short(value: Any, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _plain(value: Any) -> str:
    """A spec value as one display string: a string as itself, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _display(text: str) -> str:
    """*text* through the credential redactor, for a value a card shows in full."""
    from kiro_crew.platform import redact_via_context

    return redact_via_context(text)


def _server_spec_rows(name: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows that show one MCP server spec in full, as Apply will send it.

    Apply sends the whole spec, so the card shows the whole spec: the full
    launch line (the row wraps) and every env and header value. Agent-proposed
    values were already refused if any output redactor would change them
    (``change_cards.check_param_text``); the display pass is defence in depth.
    """
    if spec.get("url"):
        launch = str(spec["url"])
    else:
        launch = shlex.join([str(spec.get("command"))] + [str(a) for a in spec.get("args") or []])
    rows: list[dict[str, Any]] = [{"label": name, "after": _display(launch)}]
    for field, sep in (("env", "="), ("headers", ": ")):
        values = spec.get(field)
        if isinstance(values, dict) and values:
            rows.append(
                {
                    "label": f"{name} {field}",
                    "add": [f"{k}{sep}{_display(_plain(v))}" for k, v in sorted(values.items())],
                }
            )
    for field in sorted(set(spec) - {"command", "args", "url", "env", "headers"}):
        rows.append({"label": f"{name} {field}", "after": _display(_plain(spec[field]))})
    return rows


def _launched_servers(draft: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The MCP server specs a capabilities draft sets with a launch line or URL."""
    out = []
    for op in draft.get("operations") or []:
        value = op.get("value")
        if (
            op.get("section") == "mcpServers"
            and op.get("action") == "set"
            and isinstance(value, dict)
            and ("command" in value or "url" in value)
        ):
            out.append((str(op.get("id") or ""), value))
    return out


def _crewmate_put_body(fields: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """The crewmate update route's body for ``fields``, armed against a newer permission.

    An approval mode is a permission, so its write is compare-and-set: the body
    names the mode this plan read (``expected``'s, ``""`` for unset) and the
    route refuses with ``409 approval_mode_conflict``, under its config lock,
    when the stored mode has moved since. Without it a profile save made in the
    window after the card's own snapshot check and before its PUT would be
    silently replaced, possibly by a wider mode than the user last chose. The
    other fields are presentation and carry no such guard.
    """
    body = dict(fields)
    if "approval_mode" in body:
        body["expected_approval_mode"] = str(expected.get("approval_mode") or "")
    return body


def build_preview(
    kind: str, params: dict[str, Any], snapshot: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    """Title, changes, risk, scope and the apply plan for one proposal.

    *snapshot* is the gateway's read of the state the change replaces; *context*
    carries read-only facts used only for display (references, install details,
    a next run time). Raises :class:`CardCatalogError` for a proposal that would
    change nothing or cannot apply to the current state.
    """
    p = params
    # ``widen`` carries "this grants something the member runs without asking"
    # as its own flag, alongside the single ``risk`` label the badge shows. Only
    # the capabilities preview sets it today (an ``approval_expanded`` draft that
    # also launches code reads ``code_exec`` but must still require the widen
    # acknowledgement); every other kind leaves it False, so the acknowledgement
    # gate stays driven by ``risk == "widen"`` for them.
    out: dict[str, Any] = {"risk": RISK_NORMAL, "widen": False, "changes": []}
    rows: list[dict[str, Any]]
    changed: dict[str, Any]
    body: Any
    if kind == KIND_SETTING_CHANGE and "op" in p:
        if (
            snapshot.get("store") != STORE_DASHBOARD
            or snapshot.get("key") not in DASHBOARD_LIST_KEYS
        ):
            raise _fail("not_a_list", "'op'/'item' apply only to a string-list setting")
        before = snapshot.get("value")
        wording = LIST_SETTING_WORDING.get(str(snapshot.get("key")))
        label = (
            wording[0] if wording else snapshot.get("label") or p.get("setting_id") or p.get("path")
        )
        prior = string_items(before)
        after = list_after(before, p["op"], p["item"])
        if after == prior:
            state = "already lists" if p["op"] == LIST_OP_ADD else "does not list"
            raise _fail("no_change", f"'{label}' {state} '{p['item']}'")
        adding = p["op"] == LIST_OP_ADD
        if wording:
            out["title"] = (wording[1] if adding else wording[2]).format(item=p["item"])
        elif adding:
            out["title"] = f"Add “{p['item']}” to {label}"
        else:
            out["title"] = f"Remove “{p['item']}” from {label}"
        add, remove = _list_diff(prior, after)
        out["changes"] = [
            {"label": label, "before": prior, "after": after, "add": add, "remove": remove}
        ]
        # A one-item edit of a display list never widens what the agent may do.
        out["apply"] = [list_op_step(snapshot, p["op"], p["item"])]
    elif kind == KIND_SETTING_CHANGE:
        before = snapshot.get("value")
        label = snapshot.get("label") or p.get("setting_id") or p.get("path")
        if snapshot.get("store") == STORE_DASHBOARD and snapshot.get("key") in DASHBOARD_LIST_KEYS:
            raise _fail("list_setting", f"'{label}' is a list; pass 'op' and one 'item'")
        if before == p["value"]:
            raise _fail("no_change", f"'{label}' already has that value")
        out["title"] = f"Change {label}"
        out["changes"] = [{"label": label, "before": before, "after": p["value"]}]
        out["risk"] = _setting_risk(_setting_risk_key(p, snapshot), before, p["value"])
        out["apply"] = [setting_write_step(p, snapshot, p["value"])]
    elif kind == KIND_SCHEDULE_CREATE and "at" in p:
        # One-shot: the instant was resolved by the gateway's read of the zone
        # (``context["at_ts"]``) and is sent as epoch ``at``, which
        # ``POST /api/crons`` turns into a single-fire job deleted after its run.
        at_ts = context.get("at_ts")
        if isinstance(at_ts, bool) or not isinstance(at_ts, (int, float)):
            raise _fail("invalid_at", "the run time could not be resolved")
        tz = context.get("timezone") or p.get("timezone") or "UTC"
        out["title"] = f"Create one-time reminder “{p['name']}”"
        out["once"] = True
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"field": "at", "label": "When", "after": p["at"], "once": True, "timezone": tz},
            {"label": "Message", "after": _display(p["message"])},
        ]
        body = {"name": p["name"], "message": p["message"], "timezone": tz, "at": int(at_ts)}
        out["apply"] = [step("POST", "/api/crons", body)]
    elif kind == KIND_SCHEDULE_CREATE:
        out["title"] = f"Create schedule “{p['name']}”"
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"field": "cron_expr", "label": "When", "after": p["cron_expr"]},
            {"label": "Message", "after": _display(p["message"])},
        ]
        out["apply"] = [step("POST", "/api/crons", _cron_body(p))]
    elif kind == KIND_SCHEDULE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", "that schedule does not exist")
        before = snapshot.get("fields") or {}
        changed = {k: v for k, v in p["fields"].items() if before.get(k) != v}
        if not changed:
            raise _fail("no_change", "the schedule already has those values")
        out["title"] = f"Update schedule “{snapshot.get('name') or p['id']}”"
        out["changes"] = [
            {"field": k, "label": k, "before": before.get(k), "after": v}
            for k, v in changed.items()
        ]
        out["apply"] = [step("PATCH", f"/api/crons/{_q(p['id'])}", _cron_body(p["fields"]))]
    elif kind == KIND_CREWMATE_CREATE:
        if snapshot.get("exists"):
            raise _fail("already_exists", f"a crewmate named '{p['name']}' already exists")
        out["title"] = f"Create crewmate “{p['name']}”"
        out["changes"] = [
            {"label": "Name", "after": p["name"]},
            {"label": "Goal", "after": p["goal"]},
        ]
        inherited = [s for s in snapshot.get("inherited_auto_approve") or () if isinstance(s, str)]
        if inherited:
            # The crewmate runs these without asking from its first turn; the
            # click grants that, so the card says so and asks for the same
            # acknowledgement any other widening change does.
            out["changes"].append(
                {
                    "field": "auto_approve",
                    "label": "Runs without asking",
                    "after": ", ".join(inherited),
                }
            )
            out["risk"] = RISK_WIDEN
        # The goal is the crewmate's first goal in its goal record, never its
        # description. The record is addressed by the member_id the create
        # returns, so the goal is written only once the crewmate exists.
        goal_fills = [
            _step_path_fill("slug", 0, "member_id"),
            _step_fill("member", 0, "name"),
        ]
        apply = [
            step(
                "POST",
                "/api/agents",
                {
                    "name": p["name"],
                    "kiro_agent": DEFAULT_CREWMATE_TEMPLATE,
                    "source": "kirocrew",
                    # Its first chat opens with the crewmate asking about this
                    # goal, as one made on the Crewmates page does.
                    "first_greeting": True,
                },
            ),
            step(
                "PUT",
                f"/api/members/{placeholder(goal_fills[0])}/goals/long-term",
                _with_fills({"long_term": p["goal"], "expected": ""}, goal_fills[1:]),
                fill=goal_fills,
            ),
        ]
        schedule = p.get("schedule")
        if schedule:
            out["changes"].append(
                {"field": "cron_expr", "label": "When", "after": schedule["cron_expr"]}
            )
            apply.append(_create_schedule_step(p))
        out["apply"] = apply
    elif kind == KIND_CREWMATE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no crewmate named '{p['name']}'")
        stored = snapshot.get("fields") or {}
        before = dict(stored)
        # An unset approval mode is not "nothing": the thread opens in the
        # unset-effective mode, which is also what the profile's picker shows.
        # The card compares against that, so asking for the mode already in
        # force is refused as no change rather than drawn as "(empty) -> Trust".
        if "approval_mode" in p["fields"] and not before.get("approval_mode"):
            before["approval_mode"] = MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        # A model pin of "" and of DEFAULT_MODEL are one value (inherit the next
        # tier); the record stores "", the picker shows DEFAULT_MODEL. The card
        # shows the picker's spelling and compares the normalized pair, so asking
        # for the default while unpinned is no change. Reasoning effort keeps ""
        # (the dashboard labels it as inherited).
        if "model" in p["fields"]:
            before["model"] = before.get("model") or DEFAULT_MODEL
        changed = {
            k: v
            for k, v in p["fields"].items()
            if (
                normalize_agent_model(before.get(k)) != normalize_agent_model(v)
                if k == "model"
                else before.get(k) != v
            )
        }
        if not changed:
            raise _fail("no_change", "the crewmate already has those values")
        if "approval_mode" in changed:
            # A permission move is ranked like ``agent.approval_mode`` is: toward
            # ``trust`` the card is a widening (badge and acknowledgement), toward
            # ``normal`` a tightening. MEMBER_APPROVAL_MODES is ordered tightest
            # to widest, which is what makes the index comparison the ranking.
            out["risk"] = _order_risk(
                MEMBER_APPROVAL_MODES, before["approval_mode"], changed["approval_mode"]
            )
        # The title and the scope line name the crewmate the way the user knows
        # it (its display name, from the context); the key is the identity the
        # plan addresses and stays in the params and the path.
        label = str(context.get("label") or p["name"])
        out["title"] = f"Update crewmate “{label}”"
        # ``field`` lets the card draw the user's word for the field and its
        # values (the dashboard's FIELD_LABELS); ``label`` stays the fallback.
        out["changes"] = [
            {"label": k, "field": k, "before": before.get(k), "after": v}
            for k, v in changed.items()
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step("PUT", f"/api/agents/{_q(p['name'])}", _crewmate_put_body(changed, stored))
        ]
    elif kind == KIND_CREWMATE_GOAL:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no crewmate named '{p['member']}'")
        goals = [g for g in snapshot.get("goals") or [] if isinstance(g, dict)]
        label = str(context.get("label") or p["member"])
        gid = p.get("goal_id")
        goal_body: dict[str, Any] = {"member": p["member"], "long_term": p["long_term"]}
        if gid is None:
            if any(
                p["long_term"] in (g.get("text"), _goals.scrub_text(g.get("text"))) for g in goals
            ):
                raise _fail("no_change", "the crewmate already has that goal")
            goal_shown: str | None = None
            out["title"] = f"Add a goal for “{label}”"
            goal_number = len(goals) + 1
        else:
            goal_numbers = {g.get("id"): n for n, g in enumerate(goals, start=1)}
            if gid not in goal_numbers:
                raise _fail("not_found", "the crewmate has no goal with that id")
            goal_before = str(goals[goal_numbers[gid] - 1].get("text") or "")
            if goal_before == p["long_term"]:
                raise _fail("no_change", "the crewmate already has that goal")
            # The change line and the plan are public (CardStore.public): the goal
            # they replace is shown as the Goals tab shows it, scrubbed, and the
            # route takes the scrubbed text as ``expected``. The raw goal stays in
            # the gateway-private snapshot.
            goal_shown = _goals.scrub_text(goal_before)
            out["title"] = f"Set goal {goal_numbers[gid]} for “{label}”"
            goal_number = goal_numbers[gid]
            goal_body.update({"goal_id": gid, "expected": goal_shown})
        out["changes"] = [
            {
                "label": f"Goal {goal_number}",
                "number": goal_number,
                "field": "goal",
                "before": goal_shown or None,
                "after": p["long_term"],
            }
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [step("PUT", _goal_path(str(snapshot["slug"])), goal_body)]
    elif kind == KIND_CREWMATE_KEY_RESULTS:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no crewmate named '{p['member']}'")
        if not snapshot.get("has_goal"):
            raise _fail("no_goal", "the crewmate has no goal to measure yet")
        numbers = {
            g.get("id"): n
            for n, g in enumerate(snapshot.get("goals") or [], start=1)
            if isinstance(g, dict)
        }
        if any(g not in numbers for i in p["items"] for g in i.get("goal_ids", [])):
            raise _fail("not_found", "a key result names a goal the crewmate does not have")
        before_set = list(snapshot.get("key_results") or [])
        before_links = [
            list(r.get("goal_ids") or [])
            for r in snapshot.get("key_results_records") or []
            if isinstance(r, dict)
        ]
        same_links = all(
            "goal_ids" not in item
            or item["goal_ids"] == (before_links[n] if n < len(before_links) else [])
            for n, item in enumerate(p["items"])
        )
        if (
            _goals.key_result_shape(before_set) == _goals.key_result_shape(p["items"])
            and same_links
        ):
            raise _fail("no_change", "the crewmate already has those key results")
        label = str(context.get("label") or p["member"])
        # The plan and the change lines are public (CardStore.public); the set
        # they replace is shown as the Goals tab shows it, scrubbed. The route
        # takes the scrubbed set as ``expected``.
        before_set = _scrub_kr_shape(before_set)
        old_lines = [
            _kr_line(k, before_links[n] if n < len(before_links) else [], numbers)
            for n, k in enumerate(before_set)
        ]
        new_lines = [_kr_line(k, k.get("goal_ids", []), numbers) for k in p["items"]]
        out["title"] = f"Set the milestones for “{label}”"
        out["changes"] = [
            {
                "label": "Key results",
                "field": "key_results",
                "add": [ln for ln in new_lines if ln not in old_lines],
                "remove": [ln for ln in old_lines if ln not in new_lines],
            }
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step(
                "PUT",
                _key_results_path(str(snapshot["slug"])),
                {
                    "member": p["member"],
                    "items": p["items"],
                    "expected": before_set,
                    "expected_ids": [r["id"] for r in snapshot.get("key_results_records") or []],
                    "expected_links": {
                        r["id"]: sorted(r.get("goal_ids") or [])
                        for r in snapshot.get("key_results_records") or []
                    },
                },
            )
        ]
    elif kind == KIND_CREWMATE_CHECK_INS:
        if not snapshot.get("exists") or not snapshot.get("slug"):
            raise _fail("not_found", f"no crewmate named '{p['member']}'")
        before_ci = snapshot.get("schedules") or {}
        changed = {k: v for k, v in p.items() if k != "member" and before_ci.get(k) != v}
        if not changed:
            raise _fail("no_change", "the check-ins are already set that way")
        if not snapshot.get("has_goal") and any(p.get(c) is True for c in _goals.CADENCES):
            raise _fail("no_goal", "the crewmate has no goal to check in on yet")
        label = str(context.get("label") or p["member"])
        out["title"] = f"Set the check-ins for “{label}”"
        out["changes"] = [
            {
                "label": CHECK_IN_LABELS[k],
                "field": k,
                "before": _check_in_value(k, before_ci.get(k)),
                "after": _check_in_value(k, v),
            }
            for k, v in changed.items()
        ]
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        out["apply"] = [
            step(
                "PUT",
                _check_ins_path(str(snapshot["slug"])),
                {
                    "member": p["member"],
                    **changed,
                    "expected": {k: before_ci[k] for k in changed},
                },
            )
        ]
    elif kind == KIND_CREWMATE_CAPABILITIES:
        revision = snapshot.get("revision")
        if not revision:
            raise _fail("not_found", f"no capabilities to edit for '{p['member']}'")
        impact = context.get("impact") or []
        if not impact:
            raise _fail("no_change", "these operations change nothing")
        label = str(context.get("label") or p["member"])
        out["title"] = f"Change tools for “{label}”"
        out["changes"] = _impact_rows(impact)
        launched = _launched_servers(p["draft"])
        for server, spec in launched:
            # A server this card defines runs its launch line in the member's
            # next session: shown in full, and marked as running code.
            out["changes"] += _server_spec_rows(server, spec)
        out["scope"] = {"members": [label], "count": 1, "only_this_member": True}
        # An auto-approval this draft grants widens what the member runs without
        # asking, and a launch line runs third-party code. Both can be true at
        # once: a draft that sets an MCP server AND auto-approves one of its
        # tools. ``risk`` is the single worst label the badge shows, so
        # ``code_exec`` wins it; but the acknowledgement gate keys on
        # ``approval_expanded`` DIRECTLY, so carry it as its own ``widen`` flag
        # rather than folding it into ``risk`` -- otherwise a draft that does
        # both would surface as ``code_exec`` and silently drop the "runs
        # without asking" acknowledgement the plain-widen draft requires.
        approval_expanded = any(i.get("approval_expanded") for i in impact)
        out["widen"] = approval_expanded
        if launched:
            out["risk"] = RISK_CODE_EXEC
        elif approval_expanded:
            out["risk"] = RISK_WIDEN
        elif all(i.get("change") == "removed" for i in impact):
            out["risk"] = RISK_TIGHTEN
        out["apply"] = _capability_plan(p["member"], revision, p["draft"])
    elif kind == KIND_TEMPLATE_UPDATE:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no template named '{p['template']}'")
        before = snapshot.get("fields") or {}
        rows = []
        changed = {}
        widen = tighten = False
        for key, value in p["fields"].items():
            old = before.get(key)
            if old == value:
                continue
            changed[key] = value
            if isinstance(value, list):
                add, remove = _list_diff(old if isinstance(old, list) else [], value)
                rows.append({"label": key, "add": add, "remove": remove})
                if key == "allowedTools":
                    widen = widen or bool(add)
                    tighten = tighten or bool(remove)
            else:
                rows.append({"label": key, "before": old, "after": value})
        if not changed:
            raise _fail("no_change", "the template already has those values")
        out["title"] = f"Change template “{p['template']}”"
        out["changes"] = rows
        members = list(context.get("members") or [])
        out["scope"] = {"members": members, "count": len(members)}
        if widen:
            out["risk"] = RISK_WIDEN
        elif tighten:
            out["risk"] = RISK_TIGHTEN
        out["apply"] = [step("PATCH", f"/api/agents/detail/{_q(p['template'])}", changed)]
    elif kind == KIND_MCP_INSTALL:
        if snapshot.get("exists"):
            raise _fail("already_exists", f"an MCP server '{snapshot.get('name')}' already exists")
        detail = context.get("detail") or {}
        out["title"] = f"Install MCP server “{snapshot.get('name') or p['id']}”"
        out["changes"] = [
            {"label": "Source", "after": f"{p['provider']}: {p['id']}"},
            *(
                [{"label": "Command", "after": _short(detail["command"], 300)}]
                if detail.get("command")
                else []
            ),
            *([{"label": "Version", "after": detail["version"]}] if detail.get("version") else []),
            *(
                [{"label": "Repository", "after": detail["repo_url"]}]
                if detail.get("repo_url")
                else []
            ),
        ]
        out["risk"] = RISK_CODE_EXEC
        out["apply"] = [step("POST", "/api/mcp/discover/install", dict(p))]
    elif kind == KIND_MCP_ADD_CUSTOM:
        existing = [n for n, present in (snapshot.get("exists") or {}).items() if present]
        if existing:
            raise _fail("already_exists", f"an MCP server '{existing[0]}' already exists")
        out["title"] = "Add custom MCP server " + ", ".join(f"“{n}”" for n in p["servers"])
        rows = []
        for name, spec in p["servers"].items():
            rows += _server_spec_rows(name, spec)
        rows.append({"label": "Enabled", "after": p["enable"]})
        out["changes"] = rows
        out["risk"] = RISK_CODE_EXEC
        out["apply"] = [
            step("POST", "/api/mcp/custom", {"servers": p["servers"], "enable": p["enable"]})
        ]
    elif kind == KIND_MCP_TOGGLE:
        if not snapshot.get("exists"):
            raise _fail("not_found", "that MCP server is not configured")
        if snapshot.get("enabled") == p["enabled"]:
            raise _fail("no_change", "it is already in that state")
        label = f"{p['server']} / {p['tool']}" if "tool" in p else p["name"]
        verb = "Enable" if p["enabled"] else "Disable"
        out["title"] = f"{verb} MCP {'tool' if 'tool' in p else 'server'} {label}"
        out["changes"] = [
            {"label": label, "before": snapshot.get("enabled"), "after": p["enabled"]}
        ]
        out["risk"] = RISK_NORMAL if p["enabled"] else RISK_TIGHTEN
        out["apply"] = [_toggle_step(p, p["enabled"])]
    elif kind == KIND_CONNECTION_CONNECT:
        if not snapshot.get("known"):
            raise _fail("not_found", f"no connection provider '{p['slug']}'")
        if snapshot.get("granted"):
            raise _fail("no_change", f"'{p['slug']}' is already connected")
        out["title"] = f"Connect {p['slug']}"
        out["changes"] = [{"label": "Account", "before": "not connected", "after": "connected"}]
        out["apply"] = [
            step("POST", "/api/connections/mint", {"slug": p["slug"]}),
            step(
                "GET",
                "/api/connections/mint?" + urllib.parse.urlencode({"slug": p["slug"]}),
                repeat=True,
            ),
        ]
    elif kind == KIND_SECRET_SAVE:
        out["title"] = f"Save secret {p['name']}"
        out["changes"] = [
            {
                "label": p["name"],
                "before": "stored" if snapshot.get("exists") else "not stored",
                "after": "stored (value you type)",
            }
        ]
        fills = [_user_fill("value")]
        out["apply"] = [
            step("POST", "/api/secrets", _with_fills({"name": p["name"]}, fills), fill=fills)
        ]
        if snapshot.get("exists"):
            out["undo_unavailable_reason"] = "overwrites_existing"
    elif kind == KIND_TRUST_APP:
        if snapshot.get("trusted"):
            raise _fail("no_change", f"'{p['name']}' is already trusted")
        out["title"] = f"Trust app {p['name']}"
        out["changes"] = [{"label": p["name"], "before": "not trusted", "after": "trusted"}]
        if p.get("repository"):
            # Apply sends this repository as the one the person saw, so it is shown.
            out["changes"].append({"label": "Repository", "after": _display(p["repository"])})
        out["risk"] = RISK_WIDEN
        body = {"repository": p["repository"]} if p.get("repository") else None
        out["apply"] = [step("POST", f"/api/security/trusted-apps/{_q(p['name'])}", body)]
    elif kind == KIND_FOLDER_STEERING:
        if not snapshot.get("exists"):
            raise _fail("not_found", f"no folder with id '{p['folder']}'")
        current = string_items(snapshot.get("steering_dirs"))
        new = p["steering_dirs"]
        if new == current:
            raise _fail("no_change", "the folder already has that steering list")
        label = str(context.get("label") or p["folder"])
        out["title"] = f"Change steering for folder “{label}”"
        add, remove = _list_diff(current, new)
        out["changes"] = [{"label": "Folder", "after": label}]
        if add or remove:
            # The entries that differ, marked, so a dropped one is not found by eye.
            out["changes"].append({"label": "Change", "add": add, "remove": remove})
        out["changes"] += [
            {"label": "Current", "after": current},
            {"label": "New", "after": new},
        ]
        # A directory added is a host-file read the gateway now makes into every
        # chat in the folder; only removing directories narrows that.
        if add:
            out["risk"] = RISK_WIDEN
        elif remove:
            out["risk"] = RISK_TIGHTEN
        out["apply"] = [_folder_steering_step(p["folder"], new)]
    else:  # KIND_DENIED_COMMAND
        if p["action"] == "add":
            if snapshot.get("exists"):
                raise _fail("already_exists", "that denied-command pattern already exists")
            out["title"] = "Add denied-command rule"
            out["changes"] = [{"label": "Pattern", "add": [p["pattern"]]}]
            out["risk"] = RISK_TIGHTEN
            out["apply"] = [
                step(
                    "POST",
                    "/api/security/denied-commands/user",
                    {"pattern": p["pattern"], "note": p["note"]},
                )
            ]
        else:
            if not snapshot.get("exists"):
                raise _fail("not_found", "no user denied-command rule with that id")
            if snapshot.get("enabled") == p["enabled"]:
                raise _fail("no_change", "the rule is already in that state")
            verb = "Enable" if p["enabled"] else "Disable"
            out["title"] = f"{verb} denied-command rule"
            out["changes"] = [
                {
                    "label": _short(snapshot.get("pattern") or p["id"]),
                    "before": snapshot.get("enabled"),
                    "after": p["enabled"],
                }
            ]
            out["risk"] = RISK_TIGHTEN if p["enabled"] else RISK_WIDEN
            out["apply"] = [
                step(
                    "PATCH",
                    f"/api/security/denied-commands/user/{_q(p['id'])}",
                    {"enabled": p["enabled"]},
                )
            ]
    if offers_no_undo(kind, p):
        out["undo_unavailable_reason"] = UNDO_NO_IDENTITY_CHECK
    return out


#: Undo reason for kinds whose reverse is a delete their store cannot bind to the
#: entry this card wrote.
UNDO_NO_IDENTITY_CHECK = "no_identity_check"


def offers_no_undo(kind: str, params: dict[str, Any]) -> bool:
    """True for kinds whose Undo could not bind its write to the entry this card wrote.

    Uninstalling an MCP server, disconnecting an account, removing a trusted app
    and deleting a denied-command rule all go through stores that take no
    revision or fingerprint, so an Undo could not tell the entry this card wrote
    from one the user put there since under the same name. A schedule update is
    reversed by a cron PATCH, which writes unconditionally, so an Undo could
    overwrite an edit saved from another tab. These cards apply and are reversed
    from their settings page; the card offers no Undo.
    """
    if kind in (
        KIND_MCP_INSTALL,
        KIND_MCP_ADD_CUSTOM,
        KIND_CONNECTION_CONNECT,
        KIND_TRUST_APP,
        KIND_SCHEDULE_UPDATE,
    ):
        return True
    return kind == KIND_DENIED_COMMAND and params.get("action") == "add"


def _folder_steering_step(folder: str, steering_dirs: list[str]) -> dict[str, Any]:
    return step("PATCH", f"/api/chat/folders/{_q(folder)}", {"steering_dirs": steering_dirs})


def _toggle_step(p: dict[str, Any], enabled: bool) -> dict[str, Any]:
    if "tool" in p:
        return step(
            "POST",
            "/api/mcp/toggle-tool",
            {"server": p["server"], "tool": p["tool"], "enabled": enabled},
        )
    return step("POST", "/api/mcp/toggle", {"name": p["name"], "enabled": enabled})


def _create_schedule_step(p: dict[str, Any]) -> dict[str, Any]:
    """A ``crewmate.create`` card's work-schedule step, filled from step 0."""
    fills = [_step_fill("member_id", 0, "member_id")]
    body = {
        "name": f"{p['name']} schedule",
        "message": p["goal"],
        "agent": DEFAULT_CREWMATE_TEMPLATE,
        "silent": False,
        "hide_in_chat": False,
        "strict_schedule": True,
        **_cron_body(p["schedule"]),
    }
    return step("POST", "/api/crons", _with_fills(body, fills), fill=fills)


def legacy_create_apply(
    params: dict[str, Any], *, first_greeting: bool = True
) -> list[dict[str, Any]]:
    """The apply plan a ``crewmate.create`` card was stored with before goal records.

    The goal went into ``description`` and the schedule was step 1. A card
    persisted in that shape is still a valid record (``rebuild_record``): its
    applied steps and evidence keep their indexes, so its Undo still finds the
    schedule it made (:func:`_create_schedule_index`). Cards from builds whose
    create did not yet ask for a first greeting carry no ``first_greeting``
    key; ``first_greeting=False`` rebuilds that shape.
    """
    p = params
    body: dict[str, Any] = {
        "name": p["name"],
        "kiro_agent": DEFAULT_CREWMATE_TEMPLATE,
        "description": p["goal"],
        "source": "kirocrew",
    }
    if first_greeting:
        body["first_greeting"] = True
    apply = [step("POST", "/api/agents", body)]
    if p.get("schedule"):
        apply.append(_create_schedule_step(p))
    return apply


#: Where the schedule step of a legacy-shaped ``crewmate.create`` card sits.
LEGACY_CREWMATE_CREATE_SCHEDULE_STEP = 1


def _create_schedule_index(evidence: list[dict[str, Any]]) -> int:
    """The step index of a ``crewmate.create`` card's schedule, by its evidence.

    Step 1 of the current plan is the goal write, whose evidence never holds an
    ``id``; a legacy card's step 1 was the schedule, whose evidence is its job id.
    """
    legacy = len(evidence) > 1 and isinstance(evidence[1], dict) and "id" in evidence[1]
    return LEGACY_CREWMATE_CREATE_SCHEDULE_STEP if legacy else CREWMATE_CREATE_SCHEDULE_STEP


def _goal_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/long-term"


def _key_results_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/key-results"


def _check_ins_path(slug: str) -> str:
    return f"/api/members/{_q(slug)}/goals/schedules"


#: The fields a ``crewmate.check_ins`` card may set.
CHECK_IN_FIELDS = ("daily", "weekly", "monthly", "weekday", "month_day", "time")
CHECK_IN_LABELS = {
    "daily": "Daily Goal Reminder",
    "weekly": "Weekly report",
    "monthly": "Monthly review",
    "weekday": "Day of the week",
    "month_day": "Day of the month",
    "time": "Check-in time",
}
_WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _check_in_value(key: str, value: Any) -> str | None:
    """How one check-in field reads on the card."""
    if value is None:
        return None
    if key in _goals.CADENCES:
        return "On" if value else "Off"
    if key == "weekday" and isinstance(value, int) and 0 <= value <= 6:
        return _WEEKDAY_NAMES[value]
    if key == "month_day":
        return f"Day {value}"
    return str(value)


def _scrub_kr_shape(items: list[Any]) -> list[dict[str, str]]:
    from kiro_crew.member_goals import key_result_shape, scrub_text

    return [{k: scrub_text(v) for k, v in i.items()} for i in key_result_shape(items)]


def _scrub_kr_records(records: list[Any]) -> list[dict[str, Any]]:
    """Key-result records with every string through the goal scrubber."""
    from kiro_crew.member_goals import scrub_text

    out = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        clean = {k: scrub_text(v) if isinstance(v, str) else v for k, v in rec.items()}
        clean["id"] = rec.get("id")
        clean["readings"] = [
            {**r, "value": scrub_text(r.get("value", ""))}
            for r in rec.get("readings") or []
            if isinstance(r, dict)
        ]
        out.append(clean)
    return out


def _kr_line(
    item: dict[str, Any], links: list[Any] | None = None, numbers: dict[Any, int] | None = None
) -> str:
    """``text → target``, then the numbers of the goals it serves (``· #1 #3``)."""
    line = f"{item.get('text', '')} → {item.get('target', '')}"
    served = [numbers[g] for g in links or [] if numbers and g in numbers]
    if served:
        line += " · " + " ".join(f"#{n}" for n in sorted(served))
    return line


def _capability_plan(member: str, revision: str, draft: dict[str, Any]) -> list[dict[str, Any]]:
    base = {"revision": revision, **copy.deepcopy(draft)}
    fills = [_step_fill("preview_token", 0, "preview_token")]
    path = f"/api/agents/{_q(member)}/capabilities"
    return [
        step("POST", path + "/preview", base),
        step("PUT", path, _with_fills(base, fills), fill=fills),
    ]


def _impact_rows(impact: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, list[str]]] = {}
    for item in impact:
        section = str(item.get("section") or "")
        bucket = grouped.setdefault(section, {"add": [], "remove": [], "changed": []})
        change = item.get("change")
        if change == "added":
            key = "add"
        elif change == "removed":
            key = "remove"
        else:
            key = "changed"
        bucket[key].append(str(item.get("id") or ""))
    rows = []
    for section, bucket in grouped.items():
        row: dict[str, Any] = {"label": section, "add": bucket["add"], "remove": bucket["remove"]}
        if bucket["changed"]:
            row["after"] = "changed: " + ", ".join(bucket["changed"])
        rows.append(row)
    return rows


def capability_inverse(
    draft: dict[str, Any], rows: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Operations that restore each row *draft* touches to its state in *rows*.

    ``None`` when a row cannot be restored exactly: an MCP server definition the
    mate set locally is shown redacted, so writing it back would not restore it.
    """
    by_key = {(r.get("section"), r.get("id")): r for r in rows if isinstance(r, dict)}
    ops: list[dict[str, Any]] = []
    for op in draft.get("operations") or []:
        section, rid = op.get("section"), op.get("id")
        row = by_key.get((section, rid)) or {}
        state = row.get("state") or "inherited"
        if state == "removed":
            ops.append({"section": section, "id": rid, "action": "remove"})
        elif state == "local":
            if section == "mcpServers":
                return None
            ops.append({"section": section, "id": rid, "action": "set", "value": row.get("value")})
        else:
            ops.append({"section": section, "id": rid, "action": "inherit"})
    return ops


# ── evidence, undo, results ──


def extract_evidence(
    kind: str, op: str, step_index: int, data: dict[str, Any] | None
) -> dict[str, Any]:
    """The few identity fields a step's real 2xx response carried. Never values."""
    if not isinstance(data, dict):
        return {}

    def pick(*keys: str) -> dict[str, Any]:
        return {k: data[k] for k in keys if isinstance(data.get(k), (str, int, bool))}

    if kind == KIND_SCHEDULE_CREATE or (
        # Step 1 is a legacy card's schedule; the goal write it is now answers
        # no ``id``, so the pick stays empty there.
        kind == KIND_CREWMATE_CREATE
        and step_index in (LEGACY_CREWMATE_CREATE_SCHEDULE_STEP, CREWMATE_CREATE_SCHEDULE_STEP)
    ):
        return pick("id")
    if kind == KIND_CREWMATE_CREATE and step_index == 0:
        return pick("member_id", "name")
    if kind == KIND_CREWMATE_GOAL and op == OP_APPLY:
        # The goal the write added or changed, so the Undo of an added goal can
        # remove exactly that one.
        return pick("goal_id")
    if kind == KIND_CREWMATE_KEY_RESULTS and op == OP_APPLY:
        # The ids the write left, so the Undo refuses a set that reads the same
        # but was recreated since (new ids, new readings).
        krs = data.get("key_results")
        if isinstance(krs, list) and all(
            isinstance(k, dict) and isinstance(k.get("id"), str) for k in krs
        ):
            # And the goals each one serves after the write, so the Undo also
            # refuses a newer card that changed only the links.
            return {
                "ids": [k["id"] for k in krs],
                "links": {k["id"]: sorted(k.get("goal_ids") or []) for k in krs},
            }
        return {}
    if kind == KIND_CREWMATE_CAPABILITIES and step_index == 0:
        return pick("preview_token")
    if kind == KIND_CREWMATE_CAPABILITIES:
        return pick("revision")
    if kind == KIND_MCP_INSTALL:
        return pick("name")
    if kind == KIND_MCP_ADD_CUSTOM:
        added = data.get("added")
        return (
            {"added": [a for a in added if isinstance(a, str)]} if isinstance(added, list) else {}
        )
    if kind == KIND_CONNECTION_CONNECT:
        return pick("state")
    if kind == KIND_SECRET_SAVE:
        return pick("name", "revision")
    if kind == KIND_DENIED_COMMAND and op == OP_APPLY:
        return {}
    return {}


def poll_outcome(kind: str, evidence: dict[str, Any]) -> str | None:
    """For a ``repeat`` step: ``"done"``, ``"failed"`` or ``None`` (keep polling)."""
    if kind == KIND_CONNECTION_CONNECT:
        state = evidence.get("state")
        if state == "granted":
            return "done"
        if state in ("failed", "expired", "idle"):
            return "failed"
    return None


def _apply_step_is_write(step: dict[str, Any]) -> bool:
    """Whether an apply plan step writes, the same split as :data:`MEMORY_ROUTES`.

    A ``GET`` reads (the OAuth mint poll); a capability ``preview`` POST computes
    an impact and writes nothing. Every other hooked step mutates. Used so the
    preview that fronts a ``crewmate.capabilities`` apply is not miscounted as an
    applied change when its PUT never lands.
    """
    if str(step.get("method", "")).upper() == "GET":
        return False
    path = str(step.get("path", "")).partition("?")[0]
    return not path.endswith("/capabilities/preview")


def applied_write_count(apply_plan: list[dict[str, Any]], succeeded: int) -> int:
    """How many of the first *succeeded* apply steps actually wrote.

    ``succeeded`` is ``len(evidence)`` — one entry per step that returned 2xx, so
    the evidence list and the plan stay index-aligned. This collapses the read
    steps (preview, poll) out of the count passed to :func:`build_undo` as
    ``applied_steps`` WITHOUT reindexing evidence, so ``ev(i)`` still names the
    i-th step's response. A preview that succeeded while its PUT failed is zero
    writes applied and so no Undo.
    """
    return sum(1 for s in apply_plan[: max(0, succeeded)] if _apply_step_is_write(s))


def build_undo(
    kind: str,
    params: dict[str, Any],
    before: dict[str, Any],
    evidence: list[dict[str, Any]],
    applied_steps: int,
    context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """``(plan, None)`` restoring the state before the change, or ``(None, reason)``.

    *applied_steps* counts successful writes, excluding previews and polls, so
    a partial apply undoes only what took effect.
    """
    return _build_undo(kind, params, before, evidence, applied_steps, context)


def _build_undo(
    kind: str,
    params: dict[str, Any],
    before: dict[str, Any],
    evidence: list[dict[str, Any]],
    applied_steps: int,
    context: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    p = params
    ev = lambda i: evidence[i] if i < len(evidence) else {}  # noqa: E731
    if applied_steps <= 0:
        return None, "nothing_applied"
    if offers_no_undo(kind, p):
        return None, UNDO_NO_IDENTITY_CHECK
    if kind == KIND_SETTING_CHANGE and "op" in p:
        # The reverse one-item delta, through the same route the apply used.
        reverse = LIST_OP_REMOVE if p["op"] == LIST_OP_ADD else LIST_OP_ADD
        return [list_op_step(before, reverse, p["item"])], None
    if kind == KIND_SETTING_CHANGE:
        return [setting_write_step(p, before, before.get("value"))], None
    if kind == KIND_SCHEDULE_CREATE:
        job_id = ev(0).get("id")
        if not job_id:
            return None, "identity_unknown"
        return [step("DELETE", f"/api/crons/{_q(str(job_id))}")], None
    if kind == KIND_CREWMATE_CREATE:
        plan = []
        at = _create_schedule_index(evidence)
        job_id = ev(at).get("id")
        if applied_steps > at and job_id:
            plan.append(step("DELETE", f"/api/crons/{_q(str(job_id))}"))
        name = ev(0).get("name") or p["name"]
        plan.append(step("DELETE", f"/api/agents/{_q(str(name))}"))
        return plan, None
    if kind == KIND_CREWMATE_UPDATE:
        fields = before.get("fields") or {}
        old = {k: fields.get(k) if fields.get(k) is not None else "" for k in p["fields"]}
        if "approval_mode" in old:
            # The route accepts only a real mode, never "" (a pinned mode must
            # not fall back to the default by accident), so an approval mode
            # that was unset before the card is restored as the mode the thread
            # was running in, which is the same permission the user had.
            old["approval_mode"] = old["approval_mode"] or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
        # The undo's expected mode is the one the card WROTE: a profile save
        # that landed since is a newer choice, and the route's compare-and-set
        # refuses to overwrite it (409 approval_mode_conflict).
        return [
            step("PUT", f"/api/agents/{_q(p['name'])}", _crewmate_put_body(old, p["fields"]))
        ], None
    if kind == KIND_CREWMATE_GOAL:
        # Back to the goals before the card, and only while the goal still reads
        # as the card wrote it: the route's compare-and-set refuses otherwise.
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        gid = p.get("goal_id")
        if gid is None:
            # The card added a goal: its Undo removes that one (an empty text).
            gid = ev(0).get("goal_id")
            if not isinstance(gid, str):
                return None, "identity_unknown"
            old_text = ""
        else:
            old_text = next(
                (
                    str(g.get("text") or "")
                    for g in before.get("goals") or []
                    if isinstance(g, dict) and g.get("id") == gid
                ),
                "",
            )
        return [
            step(
                "PUT",
                _goal_path(str(slug)),
                {
                    "member": p["member"],
                    "goal_id": gid,
                    # Scrubbed like the key results' Undo: the plan is public.
                    "long_term": _goals.scrub_text(old_text),
                    "expected": p["long_term"],
                },
            )
        ], None
    if kind == KIND_CREWMATE_KEY_RESULTS:
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        body: dict[str, Any] = {
            "member": p["member"],
            "items": _scrub_kr_shape(list(before.get("key_results") or [])),
            # The pre-change records by id, scrubbed: the plan is public.
            # The route keeps every id still in the set as it is now and
            # puts back only the ones the card removed.
            "restore": _scrub_kr_records(list(before.get("key_results_records") or [])),
            "expected": p["items"],
        }
        ids = ev(0).get("ids")
        if isinstance(ids, list):
            # The ids the card's write left: a set recreated since with the
            # same wording carries new ids and is refused, not overwritten.
            body["expected_ids"] = ids
        links = ev(0).get("links")
        if isinstance(links, dict):
            body["expected_links"] = links
        return [step("PUT", _key_results_path(str(slug)), body)], None
    if kind == KIND_CREWMATE_CHECK_INS:
        # Each field the card changed goes back to what it was before.
        slug = before.get("slug")
        if not slug:
            return None, "identity_unknown"
        then = before.get("schedules") or {}
        back = {k: then[k] for k in CHECK_IN_FIELDS if k in p and k in then and p[k] != then[k]}
        if not back:
            return None, "identity_unknown"
        return [
            step(
                "PUT",
                _check_ins_path(str(slug)),
                {"member": p["member"], **back, "expected": {k: p[k] for k in back}},
            )
        ], None
    if kind == KIND_CREWMATE_CAPABILITIES:
        # Step 0 is a preview (not a write), step 1 is the PUT. ``applied_steps``
        # counts writes (see :func:`applied_write_count`), so a PUT that never
        # landed is ``applied_steps == 0`` and already returned "nothing_applied"
        # above. The inverse binds to the revision the PUT returned (step 1's
        # evidence), never a fresh read's — a write another tab landed after ours
        # would otherwise become the baseline and Undo would overwrite it.
        ops = capability_inverse(p["draft"], before.get("rows") or [])
        revision = ev(1).get("revision")
        if ops is None:
            return None, "redacted_server_definition"
        if not revision:
            return None, "revision_unknown"
        return _capability_plan(p["member"], revision, {"operations": ops}), None
    if kind == KIND_TEMPLATE_UPDATE:
        fields = before.get("fields") or {}
        old = {}
        for k in p["fields"]:
            value = fields.get(k)
            if value is None:
                value = [] if k != "model" else ""
            old[k] = value
        return [step("PATCH", f"/api/agents/detail/{_q(p['template'])}", old)], None
    if kind == KIND_MCP_TOGGLE:
        return [_toggle_step(p, bool(before.get("enabled")))], None
    if kind == KIND_FOLDER_STEERING:
        # Offered only while the folder holds what the card stored
        # (after_shows_the_write); the card hook arms that list for the route.
        prior_dirs = string_items(before.get("steering_dirs"))
        return [_folder_steering_step(p["folder"], prior_dirs)], None
    if kind == KIND_SECRET_SAVE:
        if before.get("exists"):
            return None, "overwrites_existing"
        # Deletes only the entry this card's save wrote: a value saved over it
        # since (another tab) has another revision and is left alone.
        revision = ev(0).get("revision")
        if not isinstance(revision, str) or not revision:
            return None, "identity_unknown"
        path = f"/api/secrets/{_q(p['name'])}?" + urllib.parse.urlencode({"if_revision": revision})
        return [step("DELETE", path)], None
    # KIND_DENIED_COMMAND toggle (an add offers no undo, see offers_no_undo)
    return [
        step(
            "PATCH",
            f"/api/security/denied-commands/user/{_q(p['id'])}",
            {"enabled": bool(before.get("enabled"))},
        )
    ], None


def undo_label(kind: str) -> str:
    """What the undo button actually does, for kinds where "Undo" would overstate it."""
    if kind == KIND_SECRET_SAVE:
        return "delete_secret"
    return "undo"


def result_summary(
    kind: str,
    params: dict[str, Any],
    status: str,
    title: str,
    *,
    failed_step: int | None = None,
) -> str:
    """One line for the collapsed card and the agent's results block.

    *failed_step* is the apply step a ``partial`` card stopped at, when known.
    """
    if status == "applied":
        return f"Done: {title}"
    if status == "partial":
        if kind == KIND_CREWMATE_CREATE:
            name = params.get("name")
            if failed_step == CREWMATE_CREATE_GOAL_STEP:
                # A failed step stops the plan, so a schedule after it never ran.
                rest = " or its schedule" if params.get("schedule") else ""
                return f"Created crewmate “{name}”, but its goal{rest} was not saved"
            if failed_step == CREWMATE_CREATE_SCHEDULE_STEP:
                return f"Created crewmate “{name}”, but its schedule was not saved"
        return f"Partly applied: {title}"
    if status == "undone":
        return f"Restored: {title}"
    if status == "failed":
        return f"Failed: {title}"
    if status == "cancelled":
        return f"Cancelled: {title}"
    if status == "expired":
        return f"Expired: {title}"
    return title


# ── manual-change memory events ──


def describe_manual_change(
    route: tuple[str, str],
    match_info: dict[str, str],
    body: Any,
    before: Any = None,
) -> str | None:
    """A concise memory line for an owner mutation, or ``None`` to record nothing.

    Names only: never a secret value, an env value, a token or an MCP spec.
    """
    method, template = route
    b = body if isinstance(body, dict) else {}
    name = match_info.get("name") or match_info.get("job_id") or match_info.get("id") or ""

    def s(value: Any, limit: int = 60) -> str:
        return _short(value, limit)

    if route == ("PATCH", "/api/config/kirocrew"):
        path = b.get("path")
        if not isinstance(path, str):
            return None
        segments = set(guide_catalog._SEGMENT_SPLIT.split(path.lower()))
        if segments & {"token", "secret", "password", "key", "credential", "credentials"}:
            return f"changed config {s(path)}"
        return f"changed config {s(path)} {s(before)}→{s(b.get('value'))}"
    if route == ("PUT", "/api/members/{slug}/goals/key-results"):
        raw_items = b.get("items")
        count = len(raw_items) if isinstance(raw_items, list) else 0
        return f"set {count} key results for crewmate {s(b.get('member'))}"
    if route == ("PUT", "/api/members/{slug}/goals/long-term"):
        return f"set the goal of crewmate {s(b.get('member'))} to {s(b.get('long_term'))}"
    if route == ("PUT", "/api/members/{slug}/goals/schedules"):
        fields = ", ".join(s(k) for k in CHECK_IN_FIELDS if k in b)
        return f"set the check-ins of crewmate {s(b.get('member'))} ({fields or 'none'})"
    if route == ("PUT", "/api/dashboard/config"):
        prior = before if isinstance(before, dict) else {}
        parts = []
        for k, v in sorted(b.items()):
            if k in DASHBOARD_KEYS:
                parts.append(f"{s(k)} {s(prior.get(k))}→{s(v)}")
            elif k in DASHBOARD_LIST_OP_KEYS:
                parts.append(f"{s(k)} {s(v)}")
            else:
                parts.append(s(k))
        return f"changed dashboard setting {', '.join(parts[:6])}" if parts else None
    if route == ("POST", "/api/crons"):
        when = b.get("cron") or b.get("every") or ("once" if b.get("at") else "")
        return f"created schedule “{s(b.get('name'))}” ({s(when)})"
    if route == ("PATCH", "/api/crons/{job_id}"):
        return f"updated schedule {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("DELETE", "/api/crons/{job_id}"):
        return f"deleted schedule {s(name)}"
    if route == ("POST", "/api/agents"):
        return f"created crewmate “{s(b.get('name'))}”"
    if route == ("PUT", "/api/agents/{name}"):
        return f"updated crewmate {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("DELETE", "/api/agents/{name}"):
        shown = before if isinstance(before, str) and before else name
        return f"deleted crewmate “{s(shown)}”"
    if route == ("PUT", "/api/agents/{name}/capabilities"):
        raw_ops = b.get("operations")
        ops: list[Any] = raw_ops if isinstance(raw_ops, list) else []
        ids = [
            f"{o.get('action')} {o.get('section')}:{o.get('id')}"
            for o in ops
            if isinstance(o, dict)
        ]
        return f"changed capabilities of {s(name)}: {s('; '.join(ids[:6]), 200)}"
    if route == ("PATCH", "/api/agents/detail/{name}"):
        return f"changed template {s(name)} ({', '.join(sorted(str(k) for k in b)[:6])})"
    if route == ("POST", "/api/mcp/discover/install"):
        return f"installed MCP server {s(b.get('id'))} from {s(b.get('provider'))}"
    if route == ("POST", "/api/mcp/custom"):
        servers = b.get("servers")
        servers = servers if isinstance(servers, dict) else {}
        return f"added custom MCP server(s) {s(', '.join(str(k) for k in servers))}"
    if route == ("POST", "/api/mcp/remove"):
        return f"removed MCP server {s(b.get('name'))}"
    if route == ("POST", "/api/mcp/apply"):
        changes = [c for c in (b.get("changes") or []) if isinstance(c, dict)]
        names = ", ".join(s(c.get("name")) for c in changes[:6])
        if changes and all(c.get("uninstall") is True for c in changes):
            return f"ran MCP uninstall for {names}"
        return f"changed MCP server scopes {names}" if names else None
    if route == ("POST", "/api/mcp/toggle"):
        return (
            f"{'enabled' if b.get('enabled', True) else 'disabled'} MCP server {s(b.get('name'))}"
        )
    if route == ("POST", "/api/mcp/toggle-tool"):
        verb = "enabled" if b.get("enabled", True) else "disabled"
        return f"{verb} MCP tool {s(b.get('server'))}/{s(b.get('tool'))}"
    if route == ("POST", "/api/connections/mint"):
        return f"started connecting {s(b.get('slug'))}"
    if route == ("POST", "/api/connections/disconnect"):
        return f"disconnected {s(b.get('slug'))}"
    if route == ("POST", "/api/secrets"):
        return f"saved secret {s(b.get('name'))}"
    if route == ("DELETE", "/api/secrets/{name}"):
        return f"deleted secret {s(name)}"
    if route == ("POST", "/api/security/trusted-apps/{name}"):
        return f"trusted app {s(name)}"
    if route == ("DELETE", "/api/security/trusted-apps/{name}"):
        return f"revoked trust for app {s(name)}"
    if route == ("POST", "/api/security/denied-commands/user"):
        return f"added denied-command rule {s(b.get('pattern'))}"
    if route == ("PATCH", "/api/security/denied-commands/user/{id}"):
        verb = "enabled" if b.get("enabled") else "disabled"
        return f"{verb} denied-command rule {s(name)}"
    if route == ("DELETE", "/api/security/denied-commands/user/{id}"):
        return f"deleted denied-command rule {s(name)}"
    del method, template
    return None


#: Every existing route a plan may reference, by ``(method, route template)``.
#: The gateway's card hook runs on exactly these. ``MEMORY_ROUTES`` is the subset
#: that mutates and so becomes a memory event; the other two are reads a plan
#: needs (a capability preview, an OAuth poll).
HOOKED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("PATCH", "/api/config/kirocrew"),
        ("PUT", "/api/dashboard/config"),
        ("POST", "/api/crons"),
        ("PATCH", "/api/crons/{job_id}"),
        ("DELETE", "/api/crons/{job_id}"),
        ("POST", "/api/agents"),
        ("PUT", "/api/agents/{name}"),
        ("DELETE", "/api/agents/{name}"),
        ("POST", "/api/agents/{name}/capabilities/preview"),
        ("PUT", "/api/agents/{name}/capabilities"),
        ("PATCH", "/api/agents/detail/{name}"),
        ("PUT", "/api/members/{slug}/goals/long-term"),
        ("PUT", "/api/members/{slug}/goals/key-results"),
        ("PUT", "/api/members/{slug}/goals/schedules"),
        ("POST", "/api/mcp/discover/install"),
        ("POST", "/api/mcp/custom"),
        ("POST", "/api/mcp/remove"),
        ("POST", "/api/mcp/apply"),
        ("POST", "/api/mcp/toggle"),
        ("POST", "/api/mcp/toggle-tool"),
        ("POST", "/api/connections/mint"),
        ("GET", "/api/connections/mint"),
        ("POST", "/api/connections/disconnect"),
        ("POST", "/api/secrets"),
        ("DELETE", "/api/secrets/{name}"),
        ("POST", "/api/security/trusted-apps/{name}"),
        ("DELETE", "/api/security/trusted-apps/{name}"),
        ("POST", "/api/security/denied-commands/user"),
        ("PATCH", "/api/security/denied-commands/user/{id}"),
        ("DELETE", "/api/security/denied-commands/user/{id}"),
        ("PATCH", "/api/chat/folders/{id}"),
    }
)
#: The folder PATCH is hooked only so a ``folder.steering`` card can be applied
#: through it; it is sidebar organization, not a setting, so it writes no
#: memory line.
MEMORY_ROUTES: frozenset[tuple[str, str]] = HOOKED_ROUTES - {
    ("POST", "/api/agents/{name}/capabilities/preview"),
    ("GET", "/api/connections/mint"),
    ("PATCH", "/api/chat/folders/{id}"),
}
