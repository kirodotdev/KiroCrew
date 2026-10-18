"""``kirocrew-core`` tool schemas for delegation, lessons and monitor loops.

The ``spawn_*`` tools, ``learn_*``, the session ledger, ``task_run``, ``file_send``,
``autonudge_stop`` and the ``monitor_*`` tools, with the wake-judge brief bounds and
:func:`validate_judge_spec`. A new ``kirocrew-core`` tool in one of these families
adds its schema here and registers it in ``server_registries``.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.config.sections import SUBAGENT_MAX_TURNS_CEILING
from kiro_crew.constants import MAX_BANNER_CHARS, MAX_SHORT_STRING

# Reasoning-effort vocabulary: ``effort.py`` is the single source of truth for
# the valid levels; EFFORT_VALUES additionally admits ``""`` ("unset — defer to
# the role pin / provider default"). Import-safe: ``effort`` pulls in only
# ``model_registry`` (stdlib-only), so no cycle back into validation.
from kiro_crew.effort import EFFORT_VALUES
from kiro_crew.monitoring.limits import MAX_RUNTIME_CEILING_SECS, validate_runtime_secs
from kiro_crew.monitoring.models import (
    MAX_MONITOR_AGENT_TURNS,
    MAX_MONITOR_CADENCE_SECS,
    MAX_MONITOR_PROVIDER_ERRORS,
    MAX_MONITOR_STOP_REASON_CHARS,
    MAX_MONITOR_TOKENS,
    MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS,
    MIN_MONITOR_CADENCE_SECS,
)
from kiro_crew.monitoring.registry import publicly_armable_kinds, publicly_armable_objectives
from kiro_crew.project_scope import SCOPE_FRAGMENT_RE
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import (
    _MODEL_NAME_RE,
    ALLOWED_LESSON_APPLIES,
    ALLOWED_LESSON_CATEGORIES,
    ALLOWED_LESSON_SCOPES,
    LESSON_LIST_LIMIT_MAX,
    LESSON_LIST_OFFSET_MAX,
    MAX_LONG_STRING,
    MAX_MEDIUM_STRING,
    REGISTERED_AGENT_NAME_RE,
    WORKSPACE_NAME_RE,
)

# ── Tool Schemas (MCP Core) ──

#: One ``spawn_run`` ``tasks[]`` entry in object form. A string
#: entry is the plain prompt; an object entry carries the prompt plus the
#: per-task overrides that win over the call's batch-wide value. Kept to the
#: fields a caller varies across one wave in practice (model, effort); a
#: per-task template is ``agents``. The rest stay batch-wide and can join this
#: object later without changing the shape again.
#: A backend's policy-facing name ("kiro", "claude", "codex"): lowercase id
#: characters only, so a request can never smuggle anything past the endpoint's
#: membership check into a log line or an error message.
_BACKEND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

SPAWN_RUN_TASK_ITEM_SCHEMA = ToolSchema(
    tool_name="spawn_run.tasks[]",
    fields=[
        FieldSpec("task", str, required=True, max_len=MAX_MEDIUM_STRING),
        FieldSpec("model", str, max_len=MAX_SHORT_STRING, pattern=_MODEL_NAME_RE),
        FieldSpec("reasoning_effort", str, allowed=EFFORT_VALUES),
        FieldSpec("backend", str, max_len=MAX_SHORT_STRING, pattern=_BACKEND_NAME_RE),
    ],
)

#: Members one spawn batch may carry (``spawn_run.tasks``, ``spawn_run.agents``,
#: ``spawn_sub_agents.agents``). A parked step retains its member list until
#: submission finishes, so the count is bounded where it is retained, by one
#: name shared by both tools. Twice the configured sub-agent ceiling of 64.
SPAWN_BATCH_MEMBERS_MAX = 128

SPAWN_RUN_SCHEMA = ToolSchema(
    tool_name="spawn_run",
    fields=[
        FieldSpec("task", str, max_len=MAX_MEDIUM_STRING),
        FieldSpec(
            "tasks",
            list,
            max_items=SPAWN_BATCH_MEMBERS_MAX,
            item_type=(str, dict),
            item_max_len=MAX_MEDIUM_STRING,
            item_schema=SPAWN_RUN_TASK_ITEM_SCHEMA,
        ),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=REGISTERED_AGENT_NAME_RE),
        FieldSpec(
            "agents",
            list,
            max_items=SPAWN_BATCH_MEMBERS_MAX,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            item_pattern=REGISTERED_AGENT_NAME_RE,
        ),
        # 0 = "not set" → falls through to config default via `0 or config_value`.
        # Bounded by the same ceiling the config loader clamps
        # ``agent.subagent_max_turns`` to, so a per-spawn override can never
        # exceed what a pinned default is allowed to be.
        FieldSpec("max_turns", int, min_val=0, max_val=SUBAGENT_MAX_TURNS_CEILING),
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
        # Optional backend (harness) for the subagent(s), spelled the way a
        # governance rule spells it ("kiro", "claude", "codex", ...). Shape only
        # here: whether it is selectable on this gateway is the spawn endpoint's
        # answer, which refuses with the selectable list.
        FieldSpec("backend", str, max_len=MAX_SHORT_STRING, pattern=_BACKEND_NAME_RE),
        # keep=True makes the run a continuable conversation: its session
        # persists (hibernated on disk) after completion, and spawn_continue
        # can dispatch follow-up turns into it with full prior context.
        FieldSpec("keep", bool),
        # Legacy solo-spawn fields: unadvertised and ignored, but accepted
        # so a skill or workflow that still sends them is not refused as
        # "unknown field".
        FieldSpec("solo_reason", str, max_len=MAX_SHORT_STRING),
        FieldSpec("solo_details", str, max_len=MAX_MEDIUM_STRING),
        # Switchable context groups the sub-agent inherits. Explicit
        # ``default=True`` rather than the implicit ``None``: the semantic
        # default is "on", and without it an explicit JSON ``null`` cleans to
        # ``None``, which a consumer coercing with ``bool()`` would read as a
        # withheld group — the opposite of what the caller asked for.
        FieldSpec("include_memory", bool, default=True),
        FieldSpec("include_lessons", bool, default=True),
        FieldSpec("include_project", bool, default=True),
        # DELEGATE TO A CREW BY NAME. A crew is a crew-member alias in
        # ``cfg.agents``; ``agent`` above is a kiro-cli template id, a disjoint
        # namespace. Naming the crew is what lets the child inherit that crew's
        # memory silo and its template together, so an orchestrator can hand work
        # to the coding crew without the email crew's memory travelling with it.
        # Resolved through ``resolve_agent_bindings``, never by deriving a store
        # from ``agent``, which answers `default` for exactly the crew that
        # configured otherwise.
        # NOT pattern-validated, for the reason SELECT_CREW_SCHEMA states above:
        # crew creation only strips the name, so a crew may legitimately contain
        # spaces or dots, and a regex here would refuse a crew the operator can
        # see in the roster. The deny-by-default gate is the `crew not in
        # cfg.agents` membership check at the endpoint, which answers 400 with an
        # `unknown_crew` code rather than degrading to the global store.
        FieldSpec("crew", str, max_len=MAX_SHORT_STRING),
        FieldSpec("target_member", str, max_len=MAX_SHORT_STRING),
    ],
)

SPAWN_CONTINUE_SCHEMA = ToolSchema(
    tool_name="spawn_continue",
    fields=[
        FieldSpec("conversation", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("task", str, required=True, max_len=MAX_MEDIUM_STRING),
        FieldSpec("agent", str, max_len=MAX_SHORT_STRING, pattern=REGISTERED_AGENT_NAME_RE),
        FieldSpec("max_turns", int, min_val=0, max_val=SUBAGENT_MAX_TURNS_CEILING),
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
        FieldSpec(
            "agents",
            list,
            required=True,
            max_items=SPAWN_BATCH_MEMBERS_MAX,
            item_type=dict,
        ),
        FieldSpec("cwd", str, max_len=MAX_MEDIUM_STRING),
        # Context groups, as on spawn_run: batch-wide, all default True.
        FieldSpec("include_memory", bool, default=True),
        FieldSpec("include_lessons", bool, default=True),
        FieldSpec("include_project", bool, default=True),
        # Retired solo-spawn gate fields, accepted and ignored as on spawn_run.
        FieldSpec("solo_reason", str, max_len=MAX_SHORT_STRING),
        FieldSpec("solo_details", str, max_len=MAX_MEDIUM_STRING),
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
        # Which startup tier the correction belongs to, as STATED by the caller.
        # Nothing infers it from category, source or wording, because none of
        # those separates a standing rule from a past finding. Absent leaves the
        # row unstated, which is served as a standing rule.
        FieldSpec("applies", str, allowed=ALLOWED_LESSON_APPLIES),
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
        # Same shape as learn_add's repo_scope: the delete selector names the
        # same stored identity the write path created, so the two share one
        # pattern. The pattern check skips an empty string, which is the
        # explicit selector for the unscoped (global) rows; an absent field
        # leaves scope out of the match entirely. A nonempty value the scope
        # gate could never satisfy (a bare "/", a dot segment) is refused here
        # so a delete cannot silently land on rows it never named.
        FieldSpec("repo_scope", str, max_len=MAX_SHORT_STRING, pattern=SCOPE_FRAGMENT_RE),
        # The JSONL tier a listed row was read from. ``learn_list`` renders it
        # because the list is a union of the global file and the active
        # workspace's, while the delete route picks ONE file from these and
        # defaults to the global one -- so a same-text workspace row could only
        # ever be named by carrying its tier back. No default: an absent field
        # keeps the route's own default rather than asserting one here.
        FieldSpec("scope", str, allowed=ALLOWED_LESSON_SCOPES),
        FieldSpec("workspace", str, max_len=MAX_SHORT_STRING, pattern=WORKSPACE_NAME_RE),
    ],
)

# The ``learn_list`` window. The bounds are the route's own (``LESSON_LIST_LIMIT``
# / ``LESSON_LIST_LIMIT_MAX``), so a value the route would clamp is refused here
# by name instead of quietly answering a different page than the one asked for.
# No defaults: an absent field keeps the route's default rather than asserting
# one here, and the body echoes the effective window either way.
LEARN_LIST_SCHEMA = ToolSchema(
    tool_name="learn_list",
    fields=[
        FieldSpec("limit", int, min_val=1, max_val=LESSON_LIST_LIMIT_MAX),
        FieldSpec("offset", int, min_val=0, max_val=LESSON_LIST_OFFSET_MAX),
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
# (a tool with no schema at all), while an empty registered schema rejects it
# (the spawn_list / resource_status precedent). The tool takes no
# arguments; the schema's job is to enforce exactly that.
SESSION_LEDGER_READ_SCHEMA = ToolSchema(tool_name="session_ledger_read")

SPAWN_STATUS_SCHEMA = ToolSchema(
    tool_name="spawn_status",
    fields=[
        FieldSpec("agent_id", str, required=True, max_len=64),
        # Paged / filtered reads of a running partial or retained full transcript
        # (line-oriented in both states).
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
        # Clamped, not rejected: a stop request must not be defeated by the
        # length of its own explanation. ``reason`` is a human-readable
        # note — it selects no behavior in ``_autonudge_stop``, which only
        # interpolates it into the applied-outcome text and the persisted stop
        # record — so truncating it costs a few words of narrative and saves
        # the stop. Rejecting would cost the stop AND fire the consumer's
        # lost-marker WARNING.
        FieldSpec("reason", str, max_len=MAX_SHORT_STRING, clamp_to_max=True),
    ],
)


def _validate_monitor_runtime(args: dict[str, Any]) -> None:
    value = args.get("max_runtime_secs")
    if value is not None:
        try:
            args["max_runtime_secs"] = validate_runtime_secs(value)
        except ValueError as exc:
            raise ValidationError("max_runtime_secs", str(exc)) from exc


#: The one value ``watch`` accepts, as a LITERAL. Spelled here rather than imported from
#: :mod:`kiro_crew.probes` for the reason that package spells its own kinds as literals:
#: this module is imported by every MCP surface and ``probes`` pulls in a probe
#: implementation, so the schema must not drag the observation layer along to validate a
#: string. ``test_both_monitor_schemas_accept_the_work_ledger_watch`` pins it equal to
#: ``probes.WORK_LEDGER``.
#:
#: CLOSED to one value on purpose. ``watch`` exists for the one subject an instruction
#: cannot name -- a session's own key is not in its own prose -- and ``gh-pr`` is already
#: inferred from the message, so accepting it here would offer a second spelling of the
#: default. A caller naming anything else is refused rather than given an ordinary timer.
_WATCH_WORK_LEDGER = "work-ledger"

#: Shared by both monitor schemas, so the arm and the revision cannot drift on what the
#: field accepts. Optional: absent means "infer the subject from the message", which is
#: every caller written before this field existed.
_MONITOR_WATCH_FIELD = FieldSpec("watch", str, allowed=frozenset({_WATCH_WORK_LEDGER}))


MONITOR_WATCH_SCHEMA = ToolSchema(
    tool_name="monitor_watch",
    custom_validator=_validate_monitor_runtime,
    fields=[
        FieldSpec("kind", str, required=True, allowed=publicly_armable_kinds()),
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("objective", str, required=True, allowed=publicly_armable_objectives()),
        FieldSpec(
            "interval_secs",
            int,
            min_val=MIN_MONITOR_CADENCE_SECS,
            max_val=MAX_MONITOR_CADENCE_SECS,
        ),
        FieldSpec("max_runtime_secs", (int, float), min_val=1, max_val=MAX_RUNTIME_CEILING_SECS),
        # Floor 0, not 1: zero is the unlimited sentinel for this one budget.
        FieldSpec("max_agent_turns", int, min_val=0, max_val=MAX_MONITOR_AGENT_TURNS),
        FieldSpec("max_tokens", int, min_val=1, max_val=MAX_MONITOR_TOKENS),
        FieldSpec("max_provider_errors", int, min_val=1, max_val=MAX_MONITOR_PROVIDER_ERRORS),
        FieldSpec("wake_instructions", str, max_len=MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS),
    ],
)

MONITOR_INSPECT_SCHEMA = ToolSchema(tool_name="monitor_inspect")
MONITOR_STOP_SCHEMA = ToolSchema(
    tool_name="monitor_stop",
    # Same shape and same reasoning as autonudge_stop's reason: a stop request
    # whose explanation runs long is still a stop request. Fixing only one of
    # the two stop tools would leave the other to be rediscovered.
    fields=[FieldSpec("reason", str, max_len=MAX_MONITOR_STOP_REASON_CHARS, clamp_to_max=True)],
)

# monitor_start creates an AutoNudge loop bound to the calling session (the
# agent-facing "babysit this PR" primitive). message caps match the REST
# endpoint's 8000-char limit; interval bounds mirror autonudge's
# _MIN_IDLE_SECS/_MAX_IDLE_SECS clamp. The custom validator applies the
# operator's finite runtime ceiling at call time.
MONITOR_START_SCHEMA = ToolSchema(
    tool_name="monitor_start",
    custom_validator=_validate_monitor_runtime,
    fields=[
        FieldSpec("message", str, required=True, max_len=8000),
        FieldSpec("interval_secs", int, min_val=15, max_val=86400),
        FieldSpec("max_cycles", int, min_val=1, max_val=1000),
        FieldSpec("max_runtime_secs", (int, float), min_val=1, max_val=MAX_RUNTIME_CEILING_SECS),
        # Opt-OUT of observation gating. Absent means gated, matching the tool's
        # default, so a caller written before this field existed keeps the
        # default behaviour rather than silently escaping it.
        FieldSpec("gate", bool),
        # The short row shown in the transcript instead of the full message. An
        # ENTRY bound only: the authorised add path re-checks the cap AFTER
        # redaction, which is the one that governs what gets stored, because
        # redaction can grow the string.
        FieldSpec("banner", str, max_len=MAX_BANNER_CHARS),
        # The wake judge's brief. A shape check only at this layer: the inner bounds
        # live in validate_judge_spec, which the arm path applies, for the reason the
        # banner cap is re-checked there -- what gets STORED is what needs bounding.
        # Accepted and stored even when the judge's consent scope is off, so an armed
        # loop survives the switch being granted later.
        #
        # ``bool`` is admitted because ``judge: false`` is the opt-out: a gated loop
        # that names no brief is screened under the default, so refusing the judge
        # needs a spelling of its own. Only ``false`` survives validate_judge_spec.
        FieldSpec("judge", (dict, bool)),
        # The SUBJECT, for the one subject a message cannot name. Accepted because
        # the whole chain carries it: the payload, the applier and the authz forward
        # all pass it through, so a request naming it is never silently discarded.
        _MONITOR_WATCH_FIELD,
    ],
)

#: Bounds for one wake-judge brief. These govern what gets STORED on the loop and
#: therefore what leaves the machine on every tick, which is why they live beside the
#: schema rather than only inside the point: the schema's ``dict`` check says the
#: field is an object, and this says the object is small.
MAX_JUDGE_TARGETS = 8
MAX_JUDGE_TARGET_CHARS = 200
MAX_JUDGE_CRITERION_CHARS = 500
#: The only keys a brief may carry. Closed, and an unknown key is REFUSED rather
#: than dropped: a misspelled ``wake_when`` that silently vanished would leave the
#: owner believing they had armed a criterion the judge never received.
JUDGE_SPEC_KEYS = frozenset({"targets", "wake_when", "quiet_when"})

#: The normalised form of ``judge: false``. A RESERVED key, deliberately absent from
#: :data:`JUDGE_SPEC_KEYS`, so the only spelling a caller has for the opt-out is the
#: boolean: an owner writing ``{"off": true}`` by hand is refused as an unknown key
#: rather than given a second way to say the same thing. The persisted loader keeps
#: the key, because it has to reload what this function stored.
JUDGE_OFF_KEY = "off"


def judge_is_off(spec: object) -> bool:
    """Whether *spec* is the stored opt-out rather than a brief. Never raises."""
    return isinstance(spec, dict) and spec.get(JUDGE_OFF_KEY) is True


def validate_judge_spec(raw: object) -> dict[str, object]:
    """One wake-judge brief, normalised and bounded, or raise :class:`ValidationError`.

    ``{}`` for an absent brief. An empty object is legal and means "no criteria of my
    own": a gated loop carrying one is screened under the DEFAULT brief, so an empty
    object is not how the judge is taken off. ``judge: false`` is -- it normalises to
    the reserved :data:`JUDGE_OFF_KEY` marker, which the tick reads as an explicit
    bypass.

    Targets are bounded and de-duplicated but NOT resolved here -- whether a
    ``chat-*`` key names a readable session is an authorization question, answered
    per tick by the creator-only read, and a target that refuses is dropped then.
    Checking it at arm time would only tell the owner what was true at arm time.
    """
    if raw is None:
        return {}
    if raw is False:
        return {JUDGE_OFF_KEY: True}
    if raw is True:
        # Refused rather than read as "use the default", because the default already
        # applies to every gated loop that names no brief. Accepting it would give one
        # meaning two spellings, and the owner who typed it is more likely to have
        # meant the opt-out.
        raise ValidationError(
            "judge", "use false to bypass the judge; the default brief needs no argument"
        )
    if not isinstance(raw, dict):
        raise ValidationError("judge", "must be an object or false")
    unknown = sorted(set(raw) - JUDGE_SPEC_KEYS)
    if unknown:
        raise ValidationError("judge", f"unknown key(s): {', '.join(unknown)}")
    out: dict[str, object] = {}
    targets = raw.get("targets")
    if targets is not None:
        if not isinstance(targets, (list, tuple)):
            raise ValidationError("judge.targets", "must be a list")
        if len(targets) > MAX_JUDGE_TARGETS:
            raise ValidationError("judge.targets", f"at most {MAX_JUDGE_TARGETS} targets")
        cleaned: list[str] = []
        for item in targets:
            if not isinstance(item, str):
                raise ValidationError("judge.targets", "every target must be a string")
            value = item.strip()
            if not value:
                continue
            if len(value) > MAX_JUDGE_TARGET_CHARS:
                raise ValidationError(
                    "judge.targets", f"a target may not exceed {MAX_JUDGE_TARGET_CHARS} chars"
                )
            if value not in cleaned:
                cleaned.append(value)
        out["targets"] = cleaned
    for key in ("wake_when", "quiet_when"):
        criterion = raw.get(key)
        if criterion is None:
            continue
        if not isinstance(criterion, str):
            raise ValidationError(f"judge.{key}", "must be a string")
        if len(criterion) > MAX_JUDGE_CRITERION_CHARS:
            raise ValidationError(
                f"judge.{key}", f"may not exceed {MAX_JUDGE_CRITERION_CHARS} chars"
            )
        out[key] = criterion
    return out


# monitor_update revises the loop already bound to the calling session. Every
# field is optional (a no-field call is a no-op the handler rejects), and the
# bounds mirror MONITOR_START_SCHEMA so a loop cannot be updated into a state
# that monitor_start would have refused to create.
MONITOR_UPDATE_SCHEMA = ToolSchema(
    tool_name="monitor_update",
    custom_validator=_validate_monitor_runtime,
    fields=[
        FieldSpec("message", str, max_len=8000),
        FieldSpec("interval_secs", int, min_val=15, max_val=86400),
        FieldSpec("max_cycles", int, min_val=1, max_val=1000),
        FieldSpec("max_runtime_secs", (int, float), min_val=1, max_val=MAX_RUNTIME_CEILING_SECS),
        FieldSpec("target", str, max_len=MAX_SHORT_STRING),
        FieldSpec("objective", str, allowed=publicly_armable_objectives()),
        # Floor 0, not 1: zero is the unlimited sentinel for this one budget.
        FieldSpec("max_agent_turns", int, min_val=0, max_val=MAX_MONITOR_AGENT_TURNS),
        FieldSpec("max_tokens", int, min_val=1, max_val=MAX_MONITOR_TOKENS),
        FieldSpec("max_provider_errors", int, min_val=1, max_val=MAX_MONITOR_PROVIDER_ERRORS),
        FieldSpec("wake_instructions", str, max_len=MAX_MONITOR_WAKE_INSTRUCTIONS_CHARS),
        # Same bound as the arm side, for the reason the comment above gives: a
        # loop must not be updatable into a state monitor_start would refuse.
        FieldSpec("banner", str, max_len=MAX_BANNER_CHARS),
        # Same shape check as the arm side, and the same reason: revising a loop must
        # not be a way to store a judge brief arming would have refused. ``judge:
        # false`` is what takes the judge off a live loop; an empty object only drops
        # the owner's own criteria, and a gated loop then runs under the default.
        FieldSpec("judge", (dict, bool)),
        # Same field as the arm side, for the reason the comment at the top of this
        # schema gives: a loop must not be updatable into a state monitor_start would
        # have refused. On this side it also ARMS a watch on a loop that has none, which
        # is the only way a conductor that armed a plain timer reaches the gate without
        # tearing its loop down and losing its cycle count.
        _MONITOR_WATCH_FIELD,
    ],
)
