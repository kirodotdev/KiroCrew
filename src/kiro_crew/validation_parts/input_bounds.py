"""The input bounds and identifier grammars every validated surface shares.

String-length tiers, fan-out and roster caps, the lesson and cron vocabularies, and
the patterns that name an agent, a template, a model, a workspace, a Slack channel or
user, a Jira key and a cron job, with the small normalizers that read them. A new
bound or identifier grammar that more than one surface checks goes here.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.lesson_validation import LESSON_APPLIES_VALUES

# ── Constants ──

# Max lengths for string inputs
MAX_TOOL_NAME_LEN = 256
MAX_SKILL_KEY_CHARS = 32768  # nested catalog keys, transported in JSON for exact reads
MAX_MEDIUM_STRING = 5_000  # messages, rules
MAX_LONG_STRING = 50_000  # task specs, inline content
# How many sessions one broadcast may reach. A fan-out bound, not a taste
# judgement: every delivery runs the full ``send_to_target`` path -- a gate, an
# audit write, and for a steer an RPC that suspends -- so an unbounded audience is
# a way to occupy the event loop for as long as the caller likes.
#
# The VALUE is tied to ``dashboard.state.MAX_SLOTS_PER_CREATOR`` (50) and must
# never fall below it. The broadcast's DEFAULT audience is "every live session
# this caller created" (``broadcast_audience``), and that set is bounded by the
# per-creator slot cap and by nothing else -- so a cap under it makes the
# documented default path refuse itself with ``too_many_targets`` as soon as a
# conductor holds more workers than the cap, which is a refusal the caller cannot
# act on: it did not name those targets, the fence did. Sitting at the per-creator
# cap makes the default audience structurally unable to exceed this bound instead
# of merely unlikely to.
#
# It is a literal rather than an import because ``kiro_crew.dashboard.state``
# imports this module (a derived value here would be a cycle), so the relation is
# held by a test instead: ``test_session_broadcast.py`` asserts
# ``MAX_BROADCAST_TARGETS >= MAX_SLOTS_PER_CREATOR``. Raise that cap and the test
# names this line; lower this one and it names it too. Note the second consumer of
# this number: ``mcp_dashboard`` sizes its one HTTP request as
# ``cap * BROADCAST_TARGET_ALLOWANCE_SECS + BROADCAST_RESPONSE_MARGIN_SECS``, so
# the worst-case broadcast request budget moves with it (50 -> 260s).
#
# It lives HERE, with the other input bounds, because the argument schema and the
# verb must refuse at the same number: two literals would let one layer enforce a
# stale cap while the other's refusal code and documentation named a different one.
MAX_BROADCAST_TARGETS = 50

# Maximum session-status rows retained for a caller. Unlike the live-slot cap,
# this bounds the durable transcript roster left by sessions that were created
# and closed, so one long-running conductor cannot grow a model-visible reply
# without limit. Applied where source rows and response rows are retained.
MAX_SESSION_STATUS_ROWS = 256

# Maximum characters retained from one session-status title. Transcript metadata
# is editable by an agent's own file tools, so this bounds attacker-controlled
# text before it enters a retained roster row and, later, a model's context.
MAX_SESSION_STATUS_TITLE_CHARS = 500

# Seconds ONE broadcast delivery may take before the loop stops waiting for it and
# moves to the next target. Enforced per delivery, never over the fan-out: the
# bound exists so a single unresponsive session cannot starve the ones behind it,
# and a shared budget the early targets could spend would do exactly that.
#
# It lives beside the cap because the two bound the same fan-out from opposite
# ends and BOTH layers read it: the backend enforces it per delivery, and the MCP
# client multiplies it by the cap to size its one HTTP request. A client budget
# below the enforced bound would let a full audience expire the request and
# discard the per-target report the verb exists to produce, so the two must move
# together -- which is what one name guarantees and two literals only hope for.
BROADCAST_TARGET_ALLOWANCE_SECS = 5.0

# Seconds the one broadcast request allows beyond the backend's worst-case
# delivery time. This covers the per-target gate and audit work, the broadcast's
# own audit write, and the HTTP response itself. It is an allowance, not a value
# derived from measurement: the client budget must EXCEED the sequential delivery
# bound, never merely equal it, so the per-target report still reaches the caller.
BROADCAST_RESPONSE_MARGIN_SECS = 10.0
# Longest backend-authored ACP session id Kiro Crew RETAINS in a store of its
# own: the native-child rosters and a created slot's frozen creator id (held in
# memory only, never written to the transcript), and through it the crew log's
# ``parent.sid`` citation. One
# constant for the whole population, applied at the point of retention: an id
# past it is dropped there, never truncated, so no store grows with a string the
# backend controls and no two stores bound the same ids differently. It lives
# here, not in the ACP layer, because application code must not import
# ``kiro_crew.acp`` and both sides of the boundary retain these ids.
MAX_ACP_SESSION_ID_LEN = 128
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


def bounded_session_id(value: object) -> "str | None":
    """*value* when it is a non-empty ACP session id within
    :data:`MAX_ACP_SESSION_ID_LEN`, else ``None``.

    The one spelling of the bound, here because more than one store retains these
    ids and a bound applied twice is a bound that can diverge -- two same-named
    private copies had already split on their sentinel before this became shared.

    The id comes from the backend, or is read back from a file an agent can write,
    so it is input rather than a fact at every retention point. An over-long or
    non-string value answers "no id" rather than a truncated one, which would name
    a different unit; and a caller that retains an unbounded string can push a
    whole record past its own maximum size, where the record is dropped rather
    than truncated, so one bad value costs a record that had nothing to do with it.

    Callers wanting an empty string rather than ``None`` spell it ``or ""`` at the
    call site, so the sentinel is the caller's choice and not a second definition.
    """
    if not isinstance(value, str) or not value or len(value) > MAX_ACP_SESSION_ID_LEN:
        return None
    return value


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
# Derived from the vocabulary module rather than restated, so a new tier cannot be
# accepted by one surface and refused by the other.
ALLOWED_LESSON_APPLIES = frozenset(LESSON_APPLIES_VALUES)

# The ``GET /api/lessons`` window: how many lessons one call returns when the
# caller names no ``limit``, and the most it may ask for. Both the route and the
# ``learn_list`` tool schema read these, so the advertised bound and the
# enforced one cannot drift apart. The read stays bounded whatever the query
# says; a caller that needs the whole population pages with ``offset`` and the
# ``total`` the body carries.
LESSON_LIST_LIMIT = 50
LESSON_LIST_LIMIT_MAX = 500
# The furthest ``offset`` either surface accepts. The vector tier hands the
# offset to SQLite as a bound parameter, and a value past the 64-bit range
# raises ``OverflowError`` there rather than returning an empty page, so the
# route clamps to this ceiling (and echoes the clamp) and the tool schema
# refuses past it. Far beyond any real population: past ``total`` every page is
# empty anyway.
LESSON_LIST_OFFSET_MAX = 100_000_000


def clamp_query_offset(offset: int) -> int:
    """``offset`` folded into ``[0, LESSON_LIST_OFFSET_MAX]``.

    The one clamp every dashboard route applies before it hands a query
    ``offset`` to a store that binds it as a SQLite parameter. The bind, not
    the query, is what makes the ceiling necessary: ``sqlite3`` raises
    ``OverflowError`` on an int past the signed 64-bit range, which a route
    that only parsed ``int(...)`` surfaces as a 500 where the caller asked for
    a page that is simply empty. A negative value is folded to ``0`` for the
    same reason -- a list route would slice from the end, a bound one would
    error. Routes that slice a Python list need no clamp: slicing already
    treats an out-of-range index as the empty tail.
    """
    return max(0, min(offset, LESSON_LIST_OFFSET_MAX))


# Allowed cron schedule kinds
ALLOWED_SCHEDULE_KINDS = frozenset({"every", "cron", "at"})

# Every event a script hook may be authored against: the five the gateway fires,
# plus the six a Kiro Agent session owns, which are stored and fired by no event
# (``hooks.HOOK_EVENTS_KAS_ONLY``, whose header says which of the six a Kiro
# Agent even asks for and why Test still runs one). Spelled
# out rather than imported from ``kiro_crew.hooks``, which imports this module --
# ``test_hook_validation_parity`` pins the two sets equal, so a member added
# there and forgotten here fails a test instead of silently refusing the new
# event at the create and update schemas.
ALLOWED_HOOK_EVENTS = frozenset(
    {
        "AgentSpawn",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "PreTaskExecution",
        "PostTaskExecution",
        "FileCreated",
        "FileEdited",
        "FileDeleted",
        "UserTriggered",
    }
)

_AGENT_NAME_RE: re.Pattern[str] = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]|[a-zA-Z0-9])\Z"
)
#: The shape ``_AGENT_NAME_RE`` (and ``WORKSPACE_NAME_RE``) admits, in words, for a
#: CLI refusal.
AGENT_NAME_HINT = (
    "expected 1-64 letters, digits, '-' or '_', starting and ending with a letter or digit"
)

TEMPLATE_NAME_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]{0,61}[A-Za-z0-9]|[A-Za-z0-9])\Z")
#: The shape ``TEMPLATE_NAME_RE`` admits, in words, for a CLI refusal.
TEMPLATE_NAME_HINT = (
    "expected 1-63 letters, digits, '.', '_' or '-', starting and ending with a letter or digit"
)

#: The union of the two identifier grammars, as one pattern for ``FieldSpec``:
#: a tool argument that names a registered agent SPEC (``spawn_run(agent=...)``,
#: ``cron_add(agent=...)``) admits a published dotted template exactly as the
#: read-side resolvers do. Crew MEMBERS are not named through these fields --
#: ``crew`` / ``member_id`` carry them, unpatterned -- so this stays an
#: identifier grammar. Keep in step with :func:`is_registered_agent_name`.
REGISTERED_AGENT_NAME_RE = re.compile(
    r"^(?:[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}[a-zA-Z0-9]"
    r"|[A-Za-z0-9][A-Za-z0-9_.-]{0,61}[A-Za-z0-9]"
    r"|[a-zA-Z0-9])\Z"
)


def is_registered_agent_name(value: object) -> bool:
    """Return whether *value* can name a registered agent spec."""
    return isinstance(value, str) and bool(
        _AGENT_NAME_RE.fullmatch(value) or TEMPLATE_NAME_RE.fullmatch(value)
    )


# Artifact slug grammar — mirrors kiro_crew.artifacts._SLUG_RE (kept here so
# consumers outside the store module share one public definition). It
# validates the companion-chat `artifact` slot binding at EVERY
# boundary it crosses: slot create (chat_handlers) and history-metadata
# restore (chat_persistence) — a tampered history JSONL must not be able to
# inject an arbitrary string that flows into to_dict()/WS broadcasts.
# \Z (not $): Python's $ also matches just before a trailing newline, so
# "valid-slug\n" would pass a $-anchored .match() — \Z anchors at the true
# end of the string.
ARTIFACT_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")

# Valid model names are an ordinary dotted and dashed identifier with at most one
# trailing bounded qualifier (``gpt-6-astra[high]``), or a provider-qualified id as a
# backend that resolves its model from its advertised list spells it
# (``provider/org/model``, ``ollama/qwen3:8b``). Every segment starts alphanumeric, so
# an id is never an option flag, an absolute path or a ``.``/``..`` hop; only the last
# segment may carry a ``:tag``. The Settings model pins share the qualified branch.
_MODEL_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]*"
QUALIFIED_MODEL_PATTERN: str = rf"{_MODEL_SEGMENT}(?:/{_MODEL_SEGMENT})+(?::{_MODEL_SEGMENT})?"
# The bound ``model_registry`` keeps on an advertised id.
_QUALIFIED_MODEL_MAX_CHARS = 256
_MODEL_NAME_RE = re.compile(
    rf"(?:{_MODEL_SEGMENT}(?:\[[A-Za-z0-9._-]+\])?"
    rf"|(?=.{{1,{_QUALIFIED_MODEL_MAX_CHARS}}}\Z){QUALIFIED_MODEL_PATTERN})\Z"
)
#: The shape ``_MODEL_NAME_RE`` admits, in words, for a CLI refusal. The
#: refusal names the shape and never echoes the rejected value.
MODEL_NAME_HINT = (
    "expected a model id of letters, digits, '.', '_' and '-' that starts with a "
    "letter or digit (optionally with one [qualifier]), a provider/model id of at "
    f"most {_QUALIFIED_MODEL_MAX_CHARS} characters, or 'auto'"
)

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
    """Map a Kiro Crew session_key to a categorical useCase label.

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
# Validates the field before it escalates send_message routing from
# notification-only to owner Slack DM delivery (a malformed/injected value
# must not abuse that upgrade).
CRON_SESSION_RE = re.compile(r"^cron:[a-zA-Z0-9]+(?::[a-zA-Z0-9]+)?$")


# Model identifiers passed to kiro-cli ``--model`` (AcpRuntime). First char
# must be alphanumeric so a value can never be parsed as a CLI flag, and the
# charset covers real model ids (gpt-5.6-sol, claude-sonnet-4.6) only.
MODEL_ID_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")
# Model identifiers stored in the model-picker VISIBILITY list
# (``dashboard.model_picker_hidden_models``). Unlike ``MODEL_ID_RE`` these are
# never passed to a CLI; they are display-only state that mirrors a provider's
# OWN advertised catalog, which carries fully-qualified ids the CLI grammar
# forbids -- ``provider/model``, ``vendor/~profile/model``, ``model:batch``,
# ``model[1m]`` and dotted/colon inference-profile ids such as
# ``anthropic.claude-sonnet-4-20250514-v1:0``. So the charset is widened to the
# punctuation those catalog ids use, while still barring whitespace and control
# characters (first char alphanumeric, bounded length) so a value can never be
# an empty/blank entry or a control-character injection.
MODEL_PICKER_HIDDEN_MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/~\[\]@-]{0,254}$")
#: Upper bound on entries in a single model-picker visibility delta. Large
#: adapter catalogs can hold many hundreds of models; this bound stays generous
#: while keeping the request size finite.
MODEL_PICKER_HIDDEN_MODELS_MAX = 2048
