"""Tool schemas and registries of the opt-in MCP servers.

Crew log, debug, work ledger, panel, guide and mate: each server's schemas and its
``MCP_*_SCHEMAS`` registry.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew import member_goals as _goals
from kiro_crew.constants import MAX_SHORT_STRING
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.work_vocab import (
    WORK_BLOCKED_REASONS,
    WORK_ITEM_STATES,
    WORK_REASON_STATUSES,
    WORK_VERDICTS,
    WORK_WORKER_STATUSES,
)

# ── Tool Schemas (MCP crew log — server ``kirocrew-crew-log``) ──
#
# Its own registry for the same reason the dashboard and work ones are separate:
# the three crew-log tools ship on an opt-in server, so their schemas do not
# belong in the always-on core registry. The registry must exist at all because
# ``call_tool_with_logging`` routes validation through it, and a tool absent from
# its server's registry has its arguments passed through raw.
#
# Every bound here is the TOOL's promise; the endpoint clamps independently. Two
# layers on purpose: a caller that asks for more than a tool will give learns so
# from the schema, and a caller that reaches the route another way still cannot
# ask the store for an unbounded read.

#: A unit argument: a raw unit id, a session/slot key, or ``self``. Sized for a
#: namespaced channel key, which is the longest legitimate form.
_CREW_LOG_UNIT_MAX = 256

CREW_LOG_LIST_SCHEMA = ToolSchema(
    tool_name="crew_log_list",
    fields=[
        FieldSpec("slot_contains", str, max_len=_CREW_LOG_UNIT_MAX),
        FieldSpec("active_within_secs", int, min_val=1, max_val=31_536_000),
        FieldSpec("with_type_counts", bool),
        FieldSpec("limit", int, min_val=1, max_val=200, default=50),
    ],
)

CREW_LOG_READ_SCHEMA = ToolSchema(
    tool_name="crew_log_read",
    fields=[
        FieldSpec("unit", str, required=True, max_len=_CREW_LOG_UNIT_MAX),
        FieldSpec("from_seq", int, min_val=1, default=1),
        FieldSpec("limit", int, min_val=1, max_val=200, default=100),
        FieldSpec("types", list, max_items=32, item_type=str, item_max_len=64),
        FieldSpec("since_ts", int, min_val=0),
        FieldSpec("full", bool),
    ],
)

CREW_LOG_PROJECTION_SCHEMA = ToolSchema(
    tool_name="crew_log_projection",
    fields=[
        FieldSpec("unit", str, required=True, max_len=_CREW_LOG_UNIT_MAX),
        FieldSpec(
            "name",
            str,
            required=True,
            # Must hold every name the tool ADVERTISES in its ``inputSchema`` enum, which is
            # ``mcp_crew_log.PROJECTION_NAMES``. Spelled literally rather than imported
            # because that module imports this one, and the two are pinned together by
            # ``test_the_projection_schema_accepts_every_advertised_fold`` so a fold added to
            # one and not the other fails CI instead of advertising a name this refuses.
            allowed=frozenset({"status", "usage", "timeline", "tools", "approvals", "subagents"}),
        ),
    ],
)

MCP_CREW_LOG_SCHEMAS: dict[str, ToolSchema] = {
    "crew_log_list": CREW_LOG_LIST_SCHEMA,
    "crew_log_read": CREW_LOG_READ_SCHEMA,
    "crew_log_projection": CREW_LOG_PROJECTION_SCHEMA,
}


# ── Tool Schemas (MCP Debug — server ``kirocrew-debug``) ──
#
# Its own registry for the same reason the crew-log one is separate: the five
# debug tools ship on an opt-in server, and a session that is not debugging a
# gateway must not pay for their schemas.
#
# What is NOT here is the load-bearing part. No schema carries a path, a pid to
# signal, a file to write, or a flag to set: every field is a QUESTION narrowing
# (a window, a filter, a format) so the surface cannot express an action. That is
# a stronger guarantee than an allowlist someone has to keep correct as fields
# are added. ``session`` is the one field naming another party, and it is a scope
# REQUEST that the route re-decides on the caller's own forwarded identity — a
# caller naming a session it may not read is refused there, not trusted here.
#
# The caps restate the server's own (``mcp_debug.MAX_SAMPLE_SECONDS``) rather than
# importing them, because ``validation`` is imported by the gateway on every
# request path and an MCP stdio server module is not; ``test_mcp_debug.py`` pins
# the two together so they cannot drift.
_DEBUG_THREAD_MODES = frozenset({"now", "sample", "dumps"})
_DEBUG_PROCESS_FORMATS = frozenset({"tree", "flat"})

#: Seconds of on-demand sampling one call may ask for. Mirrors
#: ``mcp_debug.MAX_SAMPLE_SECONDS``; the route clamps independently.
_DEBUG_MAX_SAMPLE_SECONDS = 60

#: Characters of a free-text window or filter argument. Generous for an ISO
#: timestamp or a '30m' window and far short of anything that could carry a
#: payload into a route's query string.
_DEBUG_MAX_ARG_CHARS = 128

DEBUG_GATEWAY_SCHEMA = ToolSchema(tool_name="debug_gateway")

DEBUG_REFUSALS_SCHEMA = ToolSchema(
    tool_name="debug_refusals",
    fields=[
        FieldSpec("session", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("since", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("last", int, min_val=1, max_val=1000),
    ],
)

DEBUG_THREADS_SCHEMA = ToolSchema(
    tool_name="debug_threads",
    fields=[
        FieldSpec("mode", str, allowed=_DEBUG_THREAD_MODES),
        FieldSpec("seconds", (int, float), min_val=0, max_val=_DEBUG_MAX_SAMPLE_SECONDS),
        FieldSpec("hz", int, min_val=1, max_val=1000),
        FieldSpec("deep", bool),
        # A dump NAME, never a path: the route resolves it inside the crash-dump
        # store's own directory, so a separator or a parent reference here cannot
        # address a file outside it. Bounded and pattern-checked so a traversal
        # attempt is refused at the schema rather than relied upon to fail later.
        FieldSpec(
            "read",
            str,
            max_len=_DEBUG_MAX_ARG_CHARS,
            pattern=re.compile(r"^[A-Za-z0-9._-]+$"),
        ),
    ],
)

DEBUG_PROCESSES_SCHEMA = ToolSchema(
    tool_name="debug_processes",
    fields=[
        FieldSpec("format", str, allowed=_DEBUG_PROCESS_FORMATS),
        FieldSpec("kind", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("owner", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("orphan_only", bool),
        FieldSpec("include_env", bool),
    ],
)

DEBUG_SNAPSHOTS_SCHEMA = ToolSchema(
    tool_name="debug_snapshots",
    fields=[
        FieldSpec("around", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("radius", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("since", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec("until", str, max_len=_DEBUG_MAX_ARG_CHARS),
        FieldSpec(
            "fields",
            list,
            item_type=str,
            item_max_len=64,
            max_items=64,
        ),
        FieldSpec("events_only", bool),
        FieldSpec("cursor", str, max_len=_DEBUG_MAX_ARG_CHARS),
    ],
)

MCP_DEBUG_SCHEMAS: dict[str, ToolSchema] = {
    "debug_gateway": DEBUG_GATEWAY_SCHEMA,
    "debug_refusals": DEBUG_REFUSALS_SCHEMA,
    "debug_threads": DEBUG_THREADS_SCHEMA,
    "debug_processes": DEBUG_PROCESSES_SCHEMA,
    "debug_snapshots": DEBUG_SNAPSHOTS_SCHEMA,
}


# ── Tool Schemas (MCP Work ledger — server ``kirocrew-work``) ──
#
# Its own registry for the same reason the dashboard one is separate: the four
# work-ledger tools ship on an opt-in server, and a session that is neither a
# conductor nor a worker must not pay for their schemas. The caps restate the
# store's own (``work_ledger.MAX_*``) rather than importing them, because
# ``validation`` is imported by the gateway on every request path and the store is
# not; ``test_work_ledger_tools.py`` pins the two together so they cannot drift.
#
# What is NOT here is the load-bearing part. ``WORK_REPORT_SCHEMA`` has no
# ``item_id``, no ``session``, no ``acceptance``, no ``verdict`` and no ``state``:
# a worker cannot write a conductor-owned field because no parameter carries one,
# which is a stronger guarantee than an allowlist that must be kept correct as
# fields are added.
_WORK_STATUSES = frozenset(WORK_WORKER_STATUSES)
_WORK_VERDICTS = frozenset(WORK_VERDICTS)
_WORK_ITEM_STATES = frozenset(WORK_ITEM_STATES)
#: A superset of the store's six conductor actions: ``accept`` promotes a worker's
#: claimed ``pr`` into ``acceptance`` and is served by its own store function.
_WORK_RECORD_ACTIONS = frozenset({"create", "bind", "decide", "verdict", "close", "goal", "accept"})

WORK_BRIEF_SCHEMA = ToolSchema(tool_name="work_brief")

WORK_REPORT_SCHEMA = ToolSchema(
    tool_name="work_report",
    fields=[
        FieldSpec("status", str, required=True, allowed=_WORK_STATUSES),
        # ``clamp_to_max``: the only caller is a model composing prose, which
        # cannot measure the field before it calls, so a refusal here is
        # discovered only by violating it and costs a whole round-trip to resend.
        # Clamping is safe for the same reason it is on ``monitor_stop``'s
        # ``reason``: the cut is not silent. ``clamp_to_max_len`` stamps the
        # stored value, so the conductor reads a summary that announces its own
        # truncation, and ``mcp_work.work_report`` reads the stamp back with
        # ``clamp_report`` to tell the worker in the reply. Both halves are
        # required: the stamp alone leaves the worker told only "Recorded.",
        # which is a silent loss it cannot detect.
        FieldSpec("summary", str, required=True, max_len=500, clamp_to_max=True),
        FieldSpec("artifacts", dict),
        FieldSpec("pr", int, min_val=1, max_val=1_000_000_000),
        # Why a blocked/question report is stuck. Typed so the patrol can tell a
        # wait on a person (which holds it) from one on a build (which does not);
        # the pairing with ``status`` is ``_validate_work_report``'s check.
        FieldSpec("reason", str, allowed=frozenset(WORK_BLOCKED_REASONS)),
    ],
    custom_validator=lambda cleaned: _validate_work_report(cleaned),
)

#: Every parameter of ``work_ledger_read`` narrows or shapes the read; none is
#: required, and with none the whole board comes back as it always has. The
#: ``events`` ceiling restates the route's own tail cap (``_MAX_EVENT_TAIL`` in
#: ``dashboard/handlers/work_ledger.py``), pinned together by its tests.
WORK_LEDGER_READ_SCHEMA = ToolSchema(
    tool_name="work_ledger_read",
    fields=[
        FieldSpec("events", int, min_val=0, max_val=20),
        FieldSpec("item_id", str, max_len=16, pattern=re.compile(r"^it_[0-9a-f]{8}$")),
        FieldSpec("state", str, allowed=_WORK_ITEM_STATES),
        # Long enough for an offset-carrying ISO-8601 stamp with microseconds;
        # whether it PARSES is the route's check, with the store's own reader.
        FieldSpec("since", str, max_len=40),
        FieldSpec("compact", bool),
    ],
)
WORK_LEDGER_REBUILD_SCHEMA = ToolSchema(tool_name="work_ledger_rebuild")

WORK_LEDGER_RECORD_SCHEMA: ToolSchema = ToolSchema(
    tool_name="work_ledger_record",
    fields=[
        FieldSpec("action", str, required=True, allowed=_WORK_RECORD_ACTIONS),
        # Server-minted ``it_<8 hex>``, so the pattern is what keeps a
        # model-supplied string out of a path component even before the store
        # re-checks it.
        FieldSpec("item_id", str, max_len=16, pattern=re.compile(r"^it_[0-9a-f]{8}$")),
        FieldSpec("title", str, max_len=200),
        FieldSpec("acceptance", dict),
        FieldSpec("worker_session_key", str, max_len=512),
        FieldSpec("decision", str, max_len=2000),
        FieldSpec("verdict", str, allowed=_WORK_VERDICTS),
        FieldSpec("state", str, allowed=_WORK_ITEM_STATES),
        FieldSpec("goal", str, max_len=2000),
        FieldSpec("round", int, min_val=0, max_val=1_000_000),
        FieldSpec("fails", int, min_val=0, max_val=1_000_000),
        # The store re-checks the same ranges; refusing here names the field early.
        FieldSpec("item_cap", int, min_val=1, max_val=256),
        FieldSpec("concurrency_cap", int, min_val=1, max_val=32),
        FieldSpec("new_goal", bool),
    ],
)


def _validate_work_report(cleaned: dict[str, Any]) -> None:
    """A report's cross-field checks: the artifacts map, and ``reason``'s status.

    ``reason`` says why a report is stuck, so it rides only on ``blocked`` or
    ``question``; on ``progress`` or ``done`` it would claim a wait that is not one.
    """
    _validate_work_artifacts(cleaned.get("artifacts"))
    if cleaned.get("reason") is not None and cleaned.get("status") not in WORK_REASON_STATUSES:
        raise ValidationError("reason", "is allowed only with status blocked or question")


def _validate_work_artifacts(artifacts: object) -> None:
    """Bound a ``work_report`` artifacts map: 16 keys, key 64, value 512.

    ``FieldSpec`` bounds a list's items but not a dict's, so the map's caps are
    checked here rather than being left to the store alone. Refusing at the schema
    keeps the failure a 400 that names the offending key, and the store still
    enforces the same three numbers — this is the early, specific answer, not the
    only one.
    """
    if artifacts is None:
        return
    if not isinstance(artifacts, dict):
        raise ValidationError("artifacts", "must be a JSON object")
    if len(artifacts) > 16:
        raise ValidationError("artifacts", f"exceeds max items 16 (got {len(artifacts)})")
    for key, value in artifacts.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationError("artifacts", "must map strings to strings")
        if len(key) > 64:
            raise ValidationError("artifacts", f"key {key[:32]!r} exceeds max length 64")
        if len(value) > 512:
            raise ValidationError("artifacts", f"value for {key!r} exceeds max length 512")


MCP_WORK_SCHEMAS: dict[str, ToolSchema] = {
    "work_brief": WORK_BRIEF_SCHEMA,
    "work_report": WORK_REPORT_SCHEMA,
    "work_ledger_read": WORK_LEDGER_READ_SCHEMA,
    "work_ledger_record": WORK_LEDGER_RECORD_SCHEMA,
    "work_ledger_rebuild": WORK_LEDGER_REBUILD_SCHEMA,
}


# ── Tool Schemas (MCP Panel — server ``kirocrew-panel``) ──
#
# Its own registry for the same reason the dashboard one is separate: the panel
# tools ship in an opt-in server, and a tool absent from its server's registry
# has its args passed through raw.
PANEL_PUBLISH_SCHEMA = ToolSchema(
    tool_name="panel_publish",
    fields=[
        # No max_len on the object itself — the store enforces the byte and
        # depth ceilings, because a character count over a nested structure is
        # not the bound that matters and would pass a deeply nested payload.
        FieldSpec("data", dict, required=True),
        FieldSpec("template", str, max_len=64),
        FieldSpec("title", str, max_len=200),
    ],
)
# Empty on purpose and registered on purpose: the tool takes no arguments, and
# an empty registered schema REJECTS an unexpected one, where no schema at all
# would pass it through unvalidated.
PANEL_TEMPLATES_SCHEMA = ToolSchema(tool_name="panel_templates")

#: Empty and registered, for ``PANEL_TEMPLATES_SCHEMA``'s reason: the tool takes no
#: arguments, and an empty registered schema REJECTS an unexpected one where no
#: schema at all would pass it through unvalidated.
DASHBOARD_FIELDS_SCHEMA = ToolSchema(tool_name="dashboard_fields")

DASHBOARD_WRITE_SCHEMA = ToolSchema(
    tool_name="dashboard_write",
    fields=[
        FieldSpec("field", str, required=True, max_len=64),
        # EVERY JSON SCALAR AND CONTAINER, spelled out, because the field's type is
        # the MANIFEST's to declare and not this schema's. A schema that pinned one
        # type would either contradict the manifest or restate it, and a value may
        # be a number, a phrase, a boolean, a list or an object depending on which
        # field it is.
        #
        # ``bool`` is listed FIRST and separately from the rest for a reason that
        # bit once already: a bare ``object`` here rejects ``True``, so a crewmate
        # writing into a ``boolean`` field was refused by the schema -- with a
        # message about an expected object, which is neither the real problem nor
        # something the agent could act on. ``dashboard_agentic.check_write`` is
        # the one place that knows what each field wants, and it refuses with a
        # code the mistake book can group and teach; this schema's only job is to
        # reject an unexpected ARGUMENT.
        FieldSpec("value", (bool, int, float, str, list, dict), required=True),
    ],
)

#: Why a ``dashboard_write`` call with nothing to write is refused at the MCP entry.
#: It opens with the ``value: required`` text any missing required field gets, then
#: names the remedy for the case that produces it most: an MCP client that drops an
#: empty list, an empty object or a top-level null from the arguments before they
#: reach this server, so ``value: []`` arrives as no value at all.
DASHBOARD_WRITE_NO_VALUE = (
    "required -- nothing reached this tool to write. An empty list, an empty object "
    "or null can be dropped by the MCP client before it arrives; to empty an array or "
    "object field, call dashboard_write with clear: true and no value"
)


def _validate_dashboard_write_tool(cleaned: dict[str, Any]) -> None:
    """``value`` is required unless ``clear`` is true, and the two do not mix.

    ``clear: true`` stands for the field's empty value, so a ``value`` beside it is
    accepted only when it says the same thing (``[]`` or ``{}``); the server then
    writes the empty value of the field's declared type. Any other value contradicts
    it, and which of the two the caller meant is not this check's to guess.
    """
    value = cleaned.get("value")
    if cleaned.get("clear") is True:
        if value is not None and value != [] and value != {}:
            # Reads as "clear: true writes ..." once the field name is put in front.
            raise ValidationError(
                "clear", "true writes the field's empty value, so pass it without a value"
            )
        return
    if value is None:
        raise ValidationError("value", DASHBOARD_WRITE_NO_VALUE)
    if isinstance(value, str) and not value:
        # The refusal a REQUIRED string spec gives a value that sanitizes to nothing,
        # kept word for word: ``value`` is optional in this schema only so that
        # ``clear`` can stand in for it.
        raise ValidationError("value", "required (empty after sanitization)")


#: ``dashboard_write`` as the MCP server accepts it. The gateway route validates its
#: body with ``DASHBOARD_WRITE_SCHEMA``, where ``value`` is required: the server
#: turns ``clear: true`` into the field's empty value before it posts, so the route
#: sees an ordinary write. ``clear`` exists because a boolean survives the MCP clients
#: that drop an empty container from a tool call's arguments, and array and object
#: fields have no other way to be emptied through them.
DASHBOARD_WRITE_TOOL_SCHEMA = ToolSchema(
    tool_name="dashboard_write",
    fields=[
        FieldSpec("field", str, required=True, max_len=64),
        FieldSpec("value", (bool, int, float, str, list, dict)),
        FieldSpec("clear", bool),
    ],
    custom_validator=_validate_dashboard_write_tool,
)

#: Empty and registered, for ``PANEL_TEMPLATES_SCHEMA``'s reason. The catalog is the
#: same for every caller except for the seq numbers, and those come from the calling
#: session's own identity -- so there is no argument that could narrow or widen it, and
#: an accepted one would be a filter the tool does not apply.
DASHBOARD_TYPES_SCHEMA = ToolSchema(tool_name="dashboard_types")

DASHBOARD_SAVE_SCHEMA = ToolSchema(
    tool_name="dashboard_save",
    fields=[
        # The THREE declarations and nothing else. Every other key a package has --
        # ``kind`` and ``bound_to`` -- is the gateway's to fill from the vetted
        # caller, so a caller that sends one is refused HERE by the unknown-key
        # rule every ToolSchema enforces. That refusal is the point: silently
        # dropping a caller-sent ``bound_to`` would tell an agent its page was
        # saved where it asked while the page went somewhere else.
        #
        # Typed only as objects, with no inner shape. The shape is
        # ``artifact_store.dashboard_package``'s, which holds the closed catalog
        # of data types and block types and is the one place that can name what
        # was wrong; a second shape here would either contradict it or restate it,
        # and a restatement goes stale the day the catalog grows.
        FieldSpec("model", dict, required=True),
        FieldSpec("view", dict, required=True),
        FieldSpec("theme", dict, required=True),
    ],
)

DASHBOARD_TEMPLATES_SCHEMA = ToolSchema(
    tool_name="dashboard_templates",
    fields=[
        # Optional, and the absence is a real case rather than a default: no query
        # means "list them all", which is what an agent asks for when the person has
        # not said what they want to see yet.
        FieldSpec("query", str, max_len=200),
    ],
)

DASHBOARD_PREVIEW_SCHEMA = ToolSchema(
    tool_name="dashboard_preview",
    fields=[
        # ``template_id`` is the only argument this tool acts on, and it is NOT marked
        # required: a missing one is refused by the tool with the sentence that names
        # what to call instead, where a schema refusal would name only the field.
        FieldSpec("template_id", str, max_len=64),
        # ``manifest`` and ``html`` are still ACCEPTED here, and refused one layer up.
        # A page the caller wrote cannot be previewed or adopted -- see
        # ``instance.AUTHORED_PAGE_REFUSAL`` -- and dropping these two specs would make
        # that refusal read "unknown field", which tells a caller to try a different
        # spelling rather than that the whole capability is a later change. The caps
        # bound only what reaches the refusal: ``html`` is held to 64 KiB, far under the
        # page ceiling ``MAX_INSTANCE_HTML_BYTES``, because a page sent here is refused
        # whatever its size and so never needs the room a template page does.
        FieldSpec("manifest", dict),
        FieldSpec("html", str, max_len=64 * 1024),
    ],
)

DASHBOARD_APPLY_SCHEMA = ToolSchema(
    tool_name="dashboard_apply",
    fields=[
        # Optional. Present, that template goes live -- a preview is an optional
        # look, not a gate. Absent, the crewmate's own saved package goes live.
        FieldSpec("template_id", str, max_len=64),
    ],
)

DASHBOARD_ROLLBACK_SCHEMA = ToolSchema(
    tool_name="dashboard_rollback",
    fields=[
        # No upper bound: the ceiling is whatever version this instance has reached,
        # which this schema cannot know. The store refuses a version above the current
        # one with a sentence naming the current one, which is the number the caller
        # needs; a fixed maximum here would refuse with a limit that means nothing.
        FieldSpec("to_version", int, required=True, min_val=1),
    ],
)

MCP_PANEL_SCHEMAS: dict[str, ToolSchema] = {
    "panel_publish": PANEL_PUBLISH_SCHEMA,
    "panel_templates": PANEL_TEMPLATES_SCHEMA,
    "dashboard_fields": DASHBOARD_FIELDS_SCHEMA,
    "dashboard_types": DASHBOARD_TYPES_SCHEMA,
    "dashboard_write": DASHBOARD_WRITE_TOOL_SCHEMA,
    "dashboard_save": DASHBOARD_SAVE_SCHEMA,
    "dashboard_templates": DASHBOARD_TEMPLATES_SCHEMA,
    "dashboard_preview": DASHBOARD_PREVIEW_SCHEMA,
    "dashboard_apply": DASHBOARD_APPLY_SCHEMA,
    "dashboard_rollback": DASHBOARD_ROLLBACK_SCHEMA,
}


# ── Tool Schemas (MCP Guide — server ``kirocrew-guide``) ──
#
# Its own registry because the guide tools ship in an opt-in server. The shape
# checked here is the envelope only: which action ids exist and what their params
# mean is the gateway's catalog (``guide_catalog.validate_actions``), which is the
# one place that can resolve a setting id against the packaged registry.
_GUIDE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

GUIDE_LIST_ACTIONS_SCHEMA = ToolSchema(tool_name="guide_list_actions")
GUIDE_START_SCHEMA = ToolSchema(
    tool_name="guide_start",
    fields=[
        FieldSpec("actions", list, required=True, item_type=dict, max_items=8),
        # Content rules (no links, markup, control characters) are the gateway's
        # (``guide_catalog.validate_intro``); this is only the envelope.
        FieldSpec("intro", str, max_len=200),
    ],
)
GUIDE_STATUS_SCHEMA = ToolSchema(
    tool_name="guide_status",
    fields=[FieldSpec("guide_id", str, max_len=64, pattern=_GUIDE_ID_PATTERN)],
)
GUIDE_CANCEL_SCHEMA = ToolSchema(
    tool_name="guide_cancel",
    fields=[FieldSpec("guide_id", str, required=True, max_len=64, pattern=_GUIDE_ID_PATTERN)],
)

MCP_GUIDE_SCHEMAS: dict[str, ToolSchema] = {
    "guide_list_actions": GUIDE_LIST_ACTIONS_SCHEMA,
    "guide_start": GUIDE_START_SCHEMA,
    "guide_status": GUIDE_STATUS_SCHEMA,
    "guide_cancel": GUIDE_CANCEL_SCHEMA,
    "list_change_kinds": ToolSchema(tool_name="list_change_kinds"),
    "find_setting": ToolSchema(
        tool_name="find_setting",
        fields=[FieldSpec("query", str, required=True, max_len=200)],
    ),
    "get_member_capabilities": ToolSchema(
        tool_name="get_member_capabilities",
        fields=[FieldSpec("member", str, required=True, max_len=128)],
    ),
    "diagnose_settings": ToolSchema(
        tool_name="diagnose_settings",
        fields=[FieldSpec("topic", str, max_len=200)],
    ),
    "propose_change": ToolSchema(
        tool_name="propose_change",
        fields=[
            FieldSpec("kind", str, required=True, max_len=64),
            FieldSpec("params", dict, required=True),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "get_change_status": ToolSchema(
        tool_name="get_change_status",
        fields=[
            FieldSpec("change_id", str, max_len=48, pattern=re.compile(r"^cc_[A-Za-z0-9_-]{8,40}$"))
        ],
    ),
    "search_docs": ToolSchema(
        tool_name="search_docs",
        fields=[
            FieldSpec("query", str, max_len=200),
            FieldSpec("page", str, max_len=80),
            FieldSpec("offset", int, min_val=0, max_val=10_000_000),
        ],
    ),
    "find_ui": ToolSchema(
        tool_name="find_ui",
        fields=[
            # Optional: a call passes `query` to search or `area` to browse.
            FieldSpec("query", str, max_len=200),
            FieldSpec(
                "lang",
                str,
                max_len=16,
                pattern=re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})?$"),
            ),
            FieldSpec("surface", str, max_len=64, pattern=re.compile(r"^[a-z][a-z0-9-]{0,63}$")),
            FieldSpec(
                "area",
                str,
                max_len=82,
                pattern=re.compile(r"^[a-z][a-z0-9-]{0,40}(?:\.[a-z0-9-]{1,40})?$"),
            ),
            FieldSpec("offset", int, min_val=0, max_val=100_000),
        ],
    ),
}

# ── Tool Schemas (MCP Mate — server ``kirocrew-mate``) ──
#
# A crewmate's own record. The envelope only, as for the guide: what a trait
# value means is the avatar validator's business (``config.sections._safe_avatar``),
# and a card-backed ask is validated by the change-card catalog once the gateway
# has fixed its kind and member.
MCP_MATE_SCHEMAS: dict[str, ToolSchema] = {
    "update_self": ToolSchema(
        tool_name="update_self",
        fields=[
            FieldSpec("display_name", str, max_len=MAX_SHORT_STRING),
            FieldSpec("description", str, max_len=4000),
            FieldSpec("avatar", dict),
        ],
    ),
    "set_model": ToolSchema(
        tool_name="set_model",
        fields=[
            FieldSpec("model", str, max_len=64),
            FieldSpec("reasoning_effort", str, max_len=16),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "set_approval_mode": ToolSchema(
        tool_name="set_approval_mode",
        fields=[
            FieldSpec(
                "approval_mode",
                str,
                required=True,
                max_len=16,
                allowed=frozenset({"normal", "trust_reads", "trust"}),
            ),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "find_capabilities": ToolSchema(
        tool_name="find_capabilities",
        fields=[FieldSpec("query", str, max_len=200)],
    ),
    "request_capabilities": ToolSchema(
        tool_name="request_capabilities",
        fields=[
            FieldSpec("draft", dict, required=True),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "goal_read": ToolSchema(tool_name="goal_read", fields=[]),
    "set_goal": ToolSchema(
        tool_name="set_goal",
        fields=[
            FieldSpec("long_term", str, required=True, max_len=_goals.LONG_TERM_MAX_CHARS),
            FieldSpec("goal_id", str, max_len=9),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "set_key_results": ToolSchema(
        tool_name="set_key_results",
        fields=[
            FieldSpec("items", list, required=True),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "set_check_ins": ToolSchema(
        tool_name="set_check_ins",
        fields=[
            FieldSpec("daily", bool),
            FieldSpec("weekly", bool),
            FieldSpec("monthly", bool),
            FieldSpec("heartbeat", bool),
            FieldSpec("weekday", int),
            FieldSpec("month_day", int),
            FieldSpec("time", str, max_len=5),
            FieldSpec("interval", int),
            FieldSpec("reason", str, max_len=500),
        ],
    ),
    "key_result_progress": ToolSchema(
        tool_name="key_result_progress",
        fields=[
            FieldSpec("id", str, required=True, max_len=16),
            FieldSpec("current", str, required=True, max_len=_goals.KR_READING_MAX_CHARS),
        ],
    ),
    "save_goal_report": ToolSchema(
        tool_name="save_goal_report",
        fields=[
            FieldSpec(
                "kind", str, required=True, max_len=8, allowed=frozenset({"weekly", "monthly"})
            ),
            FieldSpec("title", str, required=True, max_len=_goals.REPORT_TITLE_MAX_CHARS),
            FieldSpec("markdown", str, required=True, max_len=_goals.REPORT_MAX_CHARS),
        ],
    ),
    "save_notes": ToolSchema(
        tool_name="save_notes",
        fields=[FieldSpec("text", str, required=True, max_len=_goals.NOTES_MAX_CHARS)],
    ),
}
