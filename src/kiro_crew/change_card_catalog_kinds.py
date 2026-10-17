"""The change-card kinds an agent may propose, and the checks their parameters pass.

The kind ids, the risk and limit constants, the :data:`KINDS` table that declares each
kind's parameters, :func:`list_kinds`, and the parameter validation:
:func:`validate_params` normalizes and bounds a proposal's parameters and refuses
unknown keys, so a card can only carry the fields its kind declares, and
:func:`check_editable` refuses a re-preview that changes a field the kind does not mark
editable. A new kind, or a new check on a kind's parameters, goes here.

Served through :mod:`kiro_crew.change_card_catalog`, which callers import and tests
patch; its mirror (:mod:`kiro_crew._facade_mirror`) carries such a patch to this
module, and this module never imports it.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kiro_crew import mate_capability_catalog as _mate_catalog
from kiro_crew import member_goals as _goals
from kiro_crew.config.sections import MEMBER_APPROVAL_MODES

KIND_SETTING_CHANGE = "setting.change"
KIND_SCHEDULE_CREATE = "schedule.create"
KIND_SCHEDULE_UPDATE = "schedule.update"
KIND_CREWMATE_CREATE = "crewmate.create"
KIND_CREWMATE_UPDATE = "crewmate.update"
KIND_CREWMATE_CAPABILITIES = "crewmate.capabilities"
KIND_CREWMATE_GOAL = "crewmate.goal"
KIND_CREWMATE_KEY_RESULTS = "crewmate.key_results"
KIND_CREWMATE_CHECK_INS = "crewmate.check_ins"
#: A crewmate's one offer to write its own Dashboard page (``change_card_dashboard_page``).
KIND_CREWMATE_DASHBOARD_PAGE = "crewmate.dashboard_page"
KIND_TEMPLATE_UPDATE = "template.update"
KIND_MCP_INSTALL = "mcp.install"
KIND_MCP_ADD_CUSTOM = "mcp.add_custom"
KIND_MCP_TOGGLE = "mcp.toggle"
KIND_CONNECTION_CONNECT = "connection.connect"
KIND_SECRET_SAVE = "secret.save"
KIND_TRUST_APP = "trust.app"
KIND_DENIED_COMMAND = "denied_command"
KIND_FOLDER_STEERING = "folder.steering"
#: One crewmate from a ``.crewmate`` file
#: (``docs/request-for-change/rfc-crewmate-share.md``). The owner raises it from
#: the crewmate dropdown (``POST /api/members/import``); an agent never can, so
#: :func:`list_kinds` leaves it out.
KIND_CREWMATE_IMPORT = "crewmate.import"
#: The kinds whose apply step 0 creates a crewmate, so they share its evidence,
#: its identity-checked Undo (delete that crewmate) and its state reads.
CREWMATE_CREATING_KINDS = (KIND_CREWMATE_CREATE, KIND_CREWMATE_IMPORT)
APP_ONLY_KINDS = frozenset({KIND_CREWMATE_IMPORT})
#: Kinds only a crewmate raises about itself (``member_self.SELF_PROPOSALS``), never
#: through ``propose_change``: :func:`list_kinds` leaves them out.
SELF_ONLY_KINDS = frozenset({KIND_CREWMATE_DASHBOARD_PAGE})
#: The ``.crewmate`` ``formatVersion`` values this version reads. Any added
#: field bumps the version, so a newer file is refused rather than guessed at.
CREWMATE_FORMAT_VERSIONS = (1,)
#: The parts a ``crewmate.import`` card names as left out, as codes the
#: dashboard words: the bundle carried them and this version does not write them.
NOT_IMPORTED_CODES = ("crons", "keyResults", "avatar", "model", "dashboardTemplate")

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
#:
#: Deliberately still "kirocrew", not the mate conductor
#: (``kirocrew-mate-conductor``, rfc-mate-owned-agent-config.md migration step
#: 1): this constant is user-visible (the dialog's "Built from" dropdown label
#: and the API payload it sends), and a label/UI change is its own design
#: decision, out of scope for step 1's pure sourcing move. The mate conductor
#: is wired in only where nothing displays its name --
#: ``first_crewmate._ORDINARY_TEMPLATE``.
DEFAULT_CREWMATE_TEMPLATE = "kirocrew"

REASON_MAX_CHARS = 500
_NAME_MAX = 128
_TEXT_MAX = 4000
_CREWMATE_NAME_MAX = 24
_CREWMATE_GOAL_MAX = 200
_CREWMATE_DESCRIPTION_MAX = 1000
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
#: background-work switches (``skills.extra_paths`` is the one entry listed for
#: completeness only; see its note). Every editable path is in exactly one of this set,
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
        "dashboard.session_sort",
        # Stored only: the edition's login handler re-checks every flag against
        # its own allowlist before it runs anything.
        "dashboard.sso_login_flags",
        "dashboard.terminal.completion.enabled",
        # An allowlist id, not a command: it names which allowlisted terminal
        # app the Files tab's "Open in <app>" row starts, and only the owner's
        # own click in the desktop app, approved by the gateway's launch grant,
        # ever starts it. No value lets an agent launch anything.
        "dashboard.terminal.external_app",
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
        # Only orders *Send a copy to* (the crew it lists first); nothing is
        # sent until the person clicks a row, and no agent path reads it.
        "instances.default_destination",
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
        # Listed for exactly-one completeness, not because a card can reach it: a card
        # carries no list value (``setting.change.value`` is scalar and ``op``/``item``
        # is confined to the dashboard store), so no card proposes this path. A list of
        # skill directories can widen what an agent loads, so if cards ever carry list
        # values this moves out of here to a widening class with a type-guarded list
        # comparator.
        "skills.extra_paths",
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
    }
)

_PLACEHOLDER = "{{%s}}"


class CardCatalogError(ValueError):
    """A proposal naming an unknown kind or carrying bad parameters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


CardCatalogError.__module__ = "kiro_crew.change_card_catalog"


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
            "approval mode of its own chat (normal, trust_reads, trust), or let it "
            "request tools for its worker itself (dynamic_capabilities).",
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
                            "dynamic_capabilities": _BOOL,
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
            "all at `time` (HH:MM, 24-hour), and heartbeat (a wake every `interval` "
            f"minutes, {_goals.interval_minutes_text()}, read as "
            f"{_goals.interval_choices()}, in which the crewmate moves its "
            "work forward on its own; each run uses credits). Give only the fields "
            "that change.",
            _obj(
                {
                    "member": _STR,
                    "daily": _BOOL,
                    "weekly": _BOOL,
                    "monthly": _BOOL,
                    "heartbeat": _BOOL,
                    "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
                    "month_day": {"type": "integer", "minimum": 1, "maximum": _goals.MAX_MONTH_DAY},
                    "time": {"type": "string", "maxLength": 5},
                    "interval": {"type": "integer", "enum": list(_goals.HEARTBEAT_INTERVALS)},
                },
                ["member"],
            ),
            (
                "daily",
                "weekly",
                "monthly",
                "heartbeat",
                "weekday",
                "month_day",
                "time",
                "interval",
            ),
        ),
        KindDef(
            KIND_CREWMATE_DASHBOARD_PAGE,
            "One crewmate offers, once, to write its own Dashboard page around its goal "
            "(the crewmate's own offer_dashboard_page).",
            _obj({"member": _STR}, ["member"]),
            (),
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
        KindDef(
            KIND_CREWMATE_IMPORT,
            "Import one crewmate from a .crewmate file (owner only).",
            _obj(
                {
                    "name": {"type": "string", "maxLength": _CREWMATE_NAME_MAX},
                    "description": _STR,
                    "goal": _STR,
                    "sender": _STR,
                    "format_version": {"type": "integer"},
                    "rules": _STR,
                    "crons": {"type": "array", "items": {"type": "object"}},
                    "skills": {"type": "array", "items": {"type": "object"}},
                    "avatar_traits": {"type": "object"},
                    "model_hint": _STR,
                    "reasoning_effort_hint": _STR,
                    "dashboard_template": _STR,
                    "bundle_sha256": _STR,
                    "not_imported": {"type": "array", "items": _STR},
                    "carried": {"type": "object"},
                },
                ["name", "format_version"],
            ),
            ("name",),
        ),
    )
}


def list_kinds() -> list[dict[str, Any]]:
    """The catalog as the ``list_change_kinds`` tool returns it, app-only kinds left out."""
    return [
        {
            "id": k.id,
            "summary": k.summary,
            "params_schema": copy.deepcopy(k.params_schema),
            "editable": list(k.editable),
        }
        for k in KINDS.values()
        if k.id not in APP_ONLY_KINDS and k.id not in SELF_ONLY_KINDS
    ]


#: The fields a ``crewmate.check_ins`` card may set.
CHECK_IN_FIELDS = (
    "daily",
    "weekly",
    "monthly",
    "heartbeat",
    "weekday",
    "month_day",
    "time",
    "interval",
)


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


#: A ``.crewmate`` skill or script name (rfc-crewmate-share.md §5.2).
_IMPORT_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IMPORT_SCRIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.py:[A-Za-z_][A-Za-z0-9_]{0,63}$")
_IMPORT_SKILL_SOURCE_RE = re.compile(r"^(?:bundled|app:[a-z0-9][a-z0-9_-]{0,63})$")
_IMPORT_TEMPLATE_RE = re.compile(r"^builtin:[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMPORT_MAX_CRONS = 16
_IMPORT_MAX_SKILLS = 32


def _import_parts(p: dict[str, Any]) -> dict[str, Any]:
    """The parts of a ``crewmate.import`` card beyond its name, each as its route takes it."""
    from kiro_crew.config.sections_agent_fields import _safe_avatar
    from kiro_crew.members import MEMBER_RULES_MAX_CHARS

    out: dict[str, Any] = {}
    rules = _clean_str(p, "rules", required=False, max_len=MEMBER_RULES_MAX_CHARS, allow_empty=True)
    if rules:
        out["rules"] = rules
    crons = p.get("crons")
    if crons is not None:
        if not isinstance(crons, list) or len(crons) > _IMPORT_MAX_CRONS:
            raise _fail("invalid_param", f"'crons' must be a list of at most {_IMPORT_MAX_CRONS}")
        clean_crons = []
        for job in crons:
            if not isinstance(job, dict):
                raise _fail("invalid_param", "each cron must be an object")
            _only(job, {"name", "message", "cron_expr", "timezone", "script"}, "crons")
            expr, tz = _clean_cron(job, required=True)
            c: dict[str, Any] = {"name": _clean_str(job, "name"), "cron_expr": expr}
            if tz:
                c["timezone"] = tz
            script = _clean_str(job, "script", required=False, pattern=_IMPORT_SCRIPT_RE)
            if script:
                c["script"] = script
            message = _clean_str(job, "message", required=not script, max_len=_TEXT_MAX)
            if message:
                c["message"] = message
            clean_crons.append(c)
        if clean_crons:
            out["crons"] = clean_crons
    skills = p.get("skills")
    if skills is not None:
        if not isinstance(skills, list) or len(skills) > _IMPORT_MAX_SKILLS:
            raise _fail("invalid_param", f"'skills' must be a list of at most {_IMPORT_MAX_SKILLS}")
        refs = []
        for ref in skills:
            if not isinstance(ref, dict):
                raise _fail("invalid_param", "each skill must be an object")
            _only(ref, {"name", "source"}, "skills")
            refs.append(
                {
                    "name": _clean_str(ref, "name", pattern=_IMPORT_PART_RE),
                    "source": _clean_str(ref, "source", pattern=_IMPORT_SKILL_SOURCE_RE),
                }
            )
        if len({r["name"] for r in refs}) != len(refs):
            raise _fail("invalid_param", "'skills' names one skill twice")
        if refs:
            out["skills"] = refs
    traits = p.get("avatar_traits")
    if traits is not None:
        if not isinstance(traits, dict):
            raise _fail("invalid_param", "'avatar_traits' must be an object")
        face = _safe_avatar({"kind": "ghost", "traits": traits}).get("traits")
        if face:
            out["avatar_traits"] = face
    for key in ("model_hint", "reasoning_effort_hint"):
        hint = _clean_str(p, key, required=False, max_len=72, allow_empty=True)
        if hint:
            out[key] = hint
    template = _clean_str(p, "dashboard_template", required=False, pattern=_IMPORT_TEMPLATE_RE)
    if template:
        out["dashboard_template"] = template
    digest = _clean_str(p, "bundle_sha256", required=False, pattern=_SHA256_RE)
    if digest:
        out["bundle_sha256"] = digest
    return out


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
    if kind == KIND_CREWMATE_IMPORT:
        _only(p, set(KINDS[kind].params_schema["properties"]))
        if p.get("format_version") not in CREWMATE_FORMAT_VERSIONS:
            raise _fail("invalid_param", "'format_version' is not one this version reads")
        out = {
            "name": _member_name(p, "name", max_len=_CREWMATE_NAME_MAX),
            "format_version": p["format_version"],
        }
        for key, max_len in (("description", _CREWMATE_DESCRIPTION_MAX), ("sender", 64)):
            text = _clean_str(p, key, required=False, max_len=max_len, allow_empty=True)
            if text:
                out[key] = text
        if p.get("goal") is not None:
            out["goal"] = _goal_text(p, "goal", max_len=_LONG_TERM_GOAL_MAX, multiline=True)
        out.update(_import_parts(p))
        left_out = _str_list(p.get("not_imported", []), "not_imported", max_len=32)
        if not set(left_out) <= set(NOT_IMPORTED_CODES):
            raise _fail("invalid_param", "'not_imported' names a part this version does not know")
        if left_out:
            out["not_imported"] = left_out
        from kiro_crew import crewmate_carried

        carried = crewmate_carried.validate(p.get("carried"))
        if carried:
            if "bundle_sha256" not in out:
                raise _fail("invalid_param", "carried files need the file they came from")
            out["carried"] = carried
        return out
    if kind == KIND_CREWMATE_UPDATE:
        _only(p, {"name", "fields"})
        name = _member_name(p, "name")
        fields = p.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise _fail("invalid_param", "'fields' must be a non-empty object")
        _only(
            fields,
            {
                "description",
                "display_name",
                "model",
                "reasoning_effort",
                "approval_mode",
                "dynamic_capabilities",
            },
            "fields",
        )
        f = {}
        for key in fields:
            if key == "dynamic_capabilities":
                # The crewmate's own Dynamic capabilities preview
                # (``kiro_crew.crewmate_dynamic_capabilities``), a flag.
                f[key] = _clean_bool(fields, key)
                continue
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
        _only(p, {"member", "draft", "origins"})
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
        out_caps: dict[str, Any] = {"member": member, "draft": clean_draft}
        if "origins" in p:
            try:
                out_caps["origins"] = _mate_catalog.clean_origins(p["origins"])
            except ValueError as exc:
                raise _fail("invalid_param", str(exc)) from None
        return out_caps
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
        for key in ("daily", "weekly", "monthly", "heartbeat"):
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
        if "interval" in p:
            if not _goals.is_heartbeat_interval(p["interval"]):
                raise _fail(
                    "invalid_param",
                    "'interval' must be one of "
                    + ", ".join(str(m) for m in _goals.HEARTBEAT_INTERVALS),
                )
            out_ci["interval"] = p["interval"]
        if len(out_ci) == 1:
            raise _fail("invalid_param", "give at least one check-in or its timing")
        return out_ci
    if kind == KIND_CREWMATE_DASHBOARD_PAGE:
        _only(p, {"member"})
        return {"member": _member_name(p, "member")}
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
