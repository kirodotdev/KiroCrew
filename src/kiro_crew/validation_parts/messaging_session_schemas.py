"""Tool schemas for messaging, crews, knowledge, chat history and session control.

``send_message`` and its routing rules, notifications, Slack profile and reaction
fields, ``wait``, ``register_hook``, crew routing and selection, the knowledge and
chat-history tools, and the ``session_*`` control tools the dashboard server serves.

Callers import these names from :mod:`kiro_crew.validation`, which serves them; this
module never imports that facade.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.config.sections import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.constants import (
    CHANNEL_OWNER_DM_NAMESPACES,
    MAX_SHORT_STRING,
    SLACK_NAMESPACE,
    WAIT_TOOL_MAX_SECS,
)
from kiro_crew.validation_parts.artifact_schemas import _ARTIFACT_FOLDER_REF_MAX
from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema, ValidationError
from kiro_crew.validation_parts.input_bounds import (
    _MODEL_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_SESSION_RE,
    MAX_BROADCAST_TARGETS,
    MAX_LONG_STRING,
    MAX_MEDIUM_STRING,
    USER_ID_RE,
    USER_MAX_LEN,
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
# lists that drift. Drift here is a refusal: a pattern and an enum reading
# "discord" alone reject every other transport the gateway's channel-neutral
# owner-DM leg already serves.
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
        # Opt-in "Open session" deep-link button on the Slack leg. Declared here
        # because ``validate_tool_args`` rejects any field the descriptor
        # advertises but the schema does not declare; the gateway builds the URL
        # server-side, so nothing but this flag crosses the wire.
        FieldSpec("include_session_link", bool),
        # Must accept every value ``mcp_tools.messaging._SESSION_TARGETS``
        # advertises: this pattern runs BEFORE the handler, so a value missing
        # here is rejected as malformed even though the tool's own enum offers
        # it.
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
        FieldSpec("seconds", int, required=True, min_val=60, max_val=WAIT_TOOL_MAX_SECS),
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

ROUTE_CREW_SCHEMA = ToolSchema(
    tool_name="route_crew",
    fields=[
        FieldSpec("task", str, required=True, max_len=MAX_MEDIUM_STRING),
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
        # Organisational namespace label (items.namespace). Same 64-char cap the
        # store enforces on ingest (handlers/knowledge_sources.py). No pattern: an
        # unknown namespace just yields no results, and the value is a
        # parameterized SQL bind. It is a relevance filter, not a boundary.
        FieldSpec("namespace", str, required=False, max_len=64),
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
        # The selection namespace for ``agent``, one value: ``template``. It
        # exists for a template that a same-named crew member shadows (the stock
        # ``kirocrew-worker`` on installs where an older sync enrolled it as a
        # member): ``template`` dispatches the template on the caller's own
        # memory instead of selecting the member, which a bare name already does.
        # Omitted keeps name-first resolution; an empty string is not a namespace
        # and is refused. No ``default``: validation runs twice per call, and a
        # defaulted empty string would meet the allow-list on the second pass.
        FieldSpec("agent_kind", str, required=False, allowed=frozenset({"template"})),
        # A sidebar-folder reference — a folder id OR a ``/``-separated human
        # path, the same shape ``chat_folder_move_session.folder`` takes — so
        # filing is atomic with creation instead of a create-then-move pair a
        # folder delete can land between. Bounded like every other
        # folder reference; the two readings share no charset, so only the
        # length is checked here.
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
        # Model the new session starts on, the same field ``spawn_run.model``
        # takes and under the same charset: the id is persisted to the metadata
        # line and later handed to the backend, so arbitrary strings stay out.
        FieldSpec(
            "model",
            str,
            required=False,
            default="",
            max_len=MAX_SHORT_STRING,
            pattern=_MODEL_NAME_RE,
        ),
        # Reasoning effort the new session starts at, the same five levels
        # ``session_set_model`` takes; the verb refuses anything else. No
        # ``default``: an absent level means "inherit the dispatching
        # crewmate's", which an empty string would also read as, so the
        # schema leaves absence to the handler rather than spelling it.
        FieldSpec("reasoning_effort", str, required=False, max_len=32),
        # The new session's own compaction threshold, the override the context
        # meter's "Auto-compact at" slider sets. This range check is the early
        # refusal; create_session repeats it with the NaN guard, because the HTTP
        # route can be reached without this schema.
        FieldSpec(
            "autocompact_pct",
            (int, float),
            required=False,
            min_val=AUTOCOMPACT_PCT_MIN,
            max_val=AUTOCOMPACT_PCT_MAX,
        ),
    ],
)

SESSION_FORK_SCHEMA = ToolSchema(
    tool_name="session_fork",
    fields=[
        # The session to copy from: a slot key, transcript stem or exact unique
        # title, the same three forms every ``target`` resolves. Empty means the
        # caller's own session.
        FieldSpec("source", str, required=False, default="", max_len=MAX_SHORT_STRING),
        FieldSpec("title", str, required=False, default="", max_len=200),
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
        # The fork point, as ``chat_fork`` counts it: an index into the source's
        # visible (user/assistant) rows, inclusive. Bounded above only by the
        # transcript, which the fork core checks against the corpus it reads.
        FieldSpec("at_message_index", int, required=False, min_val=0),
    ],
)

SESSION_STOP_SCHEMA = ToolSchema(
    tool_name="session_stop",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_END_WAIT_SCHEMA = ToolSchema(
    tool_name="session_end_wait",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_RETRY_SCHEMA = ToolSchema(
    tool_name="session_retry",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_CONTINUE_SCHEMA = ToolSchema(
    tool_name="session_continue",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)


def _validate_session_set_model(args: dict[str, Any]) -> None:
    # Both are optional, but a call naming neither changes nothing. Only
    # absence counts here; the gateway refuses an empty value for either.
    if "model" not in args and "reasoning_effort" not in args:
        raise ValidationError("model", "required unless reasoning_effort is given")


SESSION_SET_MODEL_SCHEMA = ToolSchema(
    tool_name="session_set_model",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("model", str, required=False, max_len=MAX_SHORT_STRING),
        # The gateway validates the level against the set the effort dropdown
        # accepts; this cap only keeps an oversized argument off the wire.
        FieldSpec("reasoning_effort", str, required=False, max_len=32),
    ],
    custom_validator=_validate_session_set_model,
)

SESSION_RELOAD_SCHEMA = ToolSchema(
    tool_name="session_reload",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_GENERATE_TITLE_SCHEMA = ToolSchema(
    tool_name="session_generate_title",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_RENAME_SCHEMA = ToolSchema(
    tool_name="session_rename",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        # A transport bound only. The 200-character title rule is applied by the
        # route AFTER surrounding whitespace is stripped
        # (``session_control._clean_rename_title``), so a padded title that is
        # short once stripped is not refused here.
        FieldSpec("title", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

#: Swatches in the sidebar color menu. Mirrors ``PALETTE_SIZE`` in
#: ``website/src/utils/sessionColors.ts``; a test pins the two together. The
#: color route itself accepts a wider index range for older palettes, but an
#: agent is held to what the menu offers.
SESSION_PALETTE_SIZE = 7

#: ``session_set_color``'s accepted values: "none" clears, "0".."6" name the
#: sidebar color menu's swatches. The tool schema and the route's parser both
#: read this one set. The clear value is a non-empty word so that an input
#: sanitized down to "" is refused by ``required`` instead of reading as a clear.
SESSION_COLOR_CHOICES: frozenset[str] = frozenset({"none", *map(str, range(SESSION_PALETTE_SIZE))})

SESSION_SET_COLOR_SCHEMA = ToolSchema(
    tool_name="session_set_color",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("color", str, required=True, allowed=SESSION_COLOR_CHOICES),
    ],
)

SESSION_CLOSE_SCHEMA = ToolSchema(
    tool_name="session_close",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_REVIVE_SCHEMA = ToolSchema(
    tool_name="session_revive",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        # Same folder reference ``session_create.folder`` takes. The folder is
        # resolved and checked BEFORE anything is revived, so an unknown or
        # deleted folder refuses with nothing done; a revive-then-move pair
        # would leave the session revived and unfiled when the move refused.
        # Filing itself runs after the revive has committed and is best-effort.
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
    ],
)

SESSION_HISTORY_LIST_SCHEMA = ToolSchema(
    tool_name="session_history_list",
    fields=[
        # A folder id or path, resolved read-only (never created): a filter
        # naming a folder that does not exist is refused, not silently widened.
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("limit", int, required=False, default=20, min_val=1, max_val=100),
    ],
)

SESSION_SEND_SCHEMA = ToolSchema(
    tool_name="session_send",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("message", str, required=True, max_len=MAX_LONG_STRING),
        # Delivery MODE, not a permission: a true value asks for the message to be
        # injected into the target's running turn instead of waiting for the next
        # one. Strictly typed (``bool`` is an allowed type here, so the int-guard
        # in ``validate_field`` does not apply) and defaulted false, so a caller
        # that omits it keeps the queue-or-run behaviour it has today.
        FieldSpec("steer", bool, default=False),
    ],
)

#: ``work_dispatch``: ``action=create``'s two fields, ``session_create``'s fields
#: under the same bounds, the session's own title and the seed ``session_send`` takes.
WORK_DISPATCH_SCHEMA = ToolSchema(
    tool_name="work_dispatch",
    fields=[
        FieldSpec("title", str, required=True, max_len=200),
        FieldSpec("acceptance", dict, required=True),
        FieldSpec("agent", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("agent_kind", str, required=False, allowed=frozenset({"template"})),
        FieldSpec("folder", str, required=False, default="", max_len=_ARTIFACT_FOLDER_REF_MAX),
        FieldSpec("session_title", str, required=False, default="", max_len=200),
        FieldSpec("seed", str, required=True, max_len=MAX_LONG_STRING),
        FieldSpec(
            "model",
            str,
            required=False,
            default="",
            max_len=MAX_SHORT_STRING,
            pattern=_MODEL_NAME_RE,
        ),
        FieldSpec("reasoning_effort", str, required=False, max_len=32),
    ],
)

SESSION_BROADCAST_SCHEMA = ToolSchema(
    tool_name="session_broadcast",
    fields=[
        FieldSpec("message", str, required=True, max_len=MAX_LONG_STRING),
        # REQUIRED and enumerated, with no default. The two modes are different
        # instructions, not a setting with a safe side: a caller that meant "tell
        # them when they next come up for air" must not interrupt eight turns
        # because it omitted a field, and one that meant "stop, now" must not have
        # its urgency silently downgraded to the queue. So the caller states which.
        FieldSpec(
            "mode",
            str,
            required=True,
            allowed=frozenset({"queue", "steer"}),
            max_len=MAX_SHORT_STRING,
        ),
        # Names the audience. An absent, null or empty list selects nobody: the
        # backend (``broadcast_to_targets``) is the one place that refuses a call
        # with no audience, so the rule cannot drift between two copies. Bounded to
        # the same number the API enforces, so an oversized list is refused at the
        # schema with the field named rather than after a round trip.
        FieldSpec(
            "targets",
            list,
            required=False,
            item_type=str,
            item_max_len=MAX_SHORT_STRING,
            max_items=MAX_BROADCAST_TARGETS,
        ),
        # The explicit opt-in to every session this caller created. A boolean is a
        # scalar, so it survives clients that drop empty containers.
        FieldSpec("all", bool),
    ],
)

SESSION_STATUS_SCHEMA = ToolSchema(
    tool_name="session_status",
    fields=[],
)

SESSION_ADOPT_SCHEMA = ToolSchema(
    tool_name="session_adopt",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_HANDOFF_SCHEMA = ToolSchema(
    tool_name="session_handoff",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
        FieldSpec("to", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)

SESSION_RELEASE_SCHEMA = ToolSchema(
    tool_name="session_release",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
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

SESSION_SUMMARY_SCHEMA = ToolSchema(
    tool_name="session_summary",
    fields=[
        FieldSpec("target", str, required=True, max_len=MAX_SHORT_STRING),
    ],
)
