"""Messaging handlers — spawn, notifications, send-message, slack profile.

The route families are composed from the owners in ``kiro_crew.dashboard.messaging_api``;
this module stays their import path and their patch surface (see that package).
"""

from __future__ import annotations

import asyncio
import functools  # noqa: F401
import importlib.util  # noqa: F401
import inspect  # noqa: F401
import json
import logging
import math  # noqa: F401
import os  # noqa: F401
import platform  # noqa: F401
import re
import time  # noqa: F401
from collections.abc import Awaitable  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Callable, cast  # noqa: F401

from aiohttp import web

from kiro_crew import platform_compat  # noqa: F401
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.agent_sdk.drivers.acp_vocab import NATIVE_CHILD_NOT_RESUMABLE  # noqa: F401
from kiro_crew.browser.command_bus import (  # noqa: F401
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_DRAIN_WAIT_MS,
    NoPanelError,
    QueueFullError,
    get_command_bus,
)
from kiro_crew.browser_cli import install as browser_cli_install  # noqa: F401
from kiro_crew.browser_cli import install_job as browser_install_job  # noqa: F401
from kiro_crew.browser_cli import launcher as browser_cli_launcher  # noqa: F401
from kiro_crew.browser_cli import token as browser_cli_token  # noqa: F401
from kiro_crew.browser_cli import view as browser_cli_view  # noqa: F401
from kiro_crew.config import live  # noqa: F401
from kiro_crew.config import loader as _loader
from kiro_crew.config.loader import (  # noqa: F401
    IMESSAGE_SERVICES,
    TELEGRAM_ACTIVATIONS,
    KiroCrewConfig,
    config_path,
    read_config_text,
)
from kiro_crew.constants import CHANNEL_SEND_NAMESPACES, SUBAGENT_COMPLETION_META_KEY  # noqa: F401
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable  # noqa: F401
from kiro_crew.dashboard.body_numbers import body_int  # noqa: F401
from kiro_crew.dashboard.channel_folders import (  # noqa: F401
    CHANNEL_CONFIG_SECTIONS,
    channel_restart_required,
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.channel_slots import backfill_channel_folder  # noqa: F401
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _remove_queued_by_id,
    dashboard_slot_key,
    drained_to_thread,
    effective_session_key,
    mint_options_token,
    remember_slack_options,
    run_to_completion,
    slack_options_owner_key,
    slot_history_key,
    subagent_event_slot,
)
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    _pip_install_channel_available,
    guard_owner_surface_routes,
    internal_memory_scope,
    pip_extra_install_command,
    read_bounded_json,
    spawn_parent_claim,
)
from kiro_crew.dashboard.handlers.browser_view_relay import ROUTE_PREFIX  # noqa: F401
from kiro_crew.dashboard.handlers.core import _hot_apply_after_write  # noqa: F401
from kiro_crew.dashboard.messaging_api import agent_push as _owner_agent_push
from kiro_crew.dashboard.messaging_api import browser_routes as _owner_browser_routes
from kiro_crew.dashboard.messaging_api import channel_config_writes as _owner_channel_config_writes
from kiro_crew.dashboard.messaging_api import channel_delivery as _owner_channel_delivery
from kiro_crew.dashboard.messaging_api import (
    channel_folder_backfill as _owner_channel_folder_backfill,
)
from kiro_crew.dashboard.messaging_api import discord_settings as _owner_discord_settings
from kiro_crew.dashboard.messaging_api import feishu_settings as _owner_feishu_settings
from kiro_crew.dashboard.messaging_api import imessage_settings as _owner_imessage_settings
from kiro_crew.dashboard.messaging_api import notifications as _owner_notifications
from kiro_crew.dashboard.messaging_api import proactive_send as _owner_proactive_send
from kiro_crew.dashboard.messaging_api import run_control as _owner_run_control
from kiro_crew.dashboard.messaging_api import run_views as _owner_run_views
from kiro_crew.dashboard.messaging_api import slack_settings as _owner_slack_settings
from kiro_crew.dashboard.messaging_api import slack_workspace as _owner_slack_workspace
from kiro_crew.dashboard.messaging_api import spawn as _owner_spawn
from kiro_crew.dashboard.messaging_api import teams_settings as _owner_teams_settings
from kiro_crew.dashboard.messaging_api import telegram_settings as _owner_telegram_settings
from kiro_crew.dashboard.messaging_api import webex_settings as _owner_webex_settings
from kiro_crew.dashboard.messaging_api import wecom_settings as _owner_wecom_settings
from kiro_crew.dashboard.messaging_api.agent_push import (  # noqa: F401
    api_notification_agent_push,
)
from kiro_crew.dashboard.messaging_api.browser_routes import (  # noqa: F401
    _browser_install_active,
    _browser_install_conflict,
    _browser_install_job,
    _browser_install_status,
    _browser_view_payload,
    _deny_non_owner_browser_request,
    _start_browser_install_job,
    _terminate_install_scope,
    api_browser_command,
    api_browser_command_drain,
    api_browser_command_result,
    api_browser_engine_install,
    api_browser_install_get,
    api_browser_install_start,
    api_browser_open,
    api_browser_token_put,
    api_browser_view_get,
    api_browser_view_start,
    stop_browser_install,
)
from kiro_crew.dashboard.messaging_api.channel_config_writes import (  # noqa: F401
    _clean_id_list,
    _LockedSectionWrite,
    _mask_secret,
    _threshold_pct_rejection,
    _wide_env_refusal,
    _write_env_or_roll_back,
    _write_env_updates,
    _write_env_updates_locked,
)
from kiro_crew.dashboard.messaging_api.channel_delivery import (  # noqa: F401
    _channel_delivery_key,
    _ChannelSendFailed,
    _cron_job_for,
    _deliver_channel_dm,
    _deliver_to_channel,
    _owner_dm_target,
    _send_to_channel_target,
    _vet_channel_send,
)
from kiro_crew.dashboard.messaging_api.channel_folder_backfill import (  # noqa: F401
    api_channel_folder_backfill,
)
from kiro_crew.dashboard.messaging_api.discord_settings import (  # noqa: F401
    _discord_config_save_locked,
    _validate_discord_token,
    api_discord_config_get,
    api_discord_config_save,
)
from kiro_crew.dashboard.messaging_api.feishu_settings import (  # noqa: F401
    _channel_sdk_status,
    _feishu_config_save_locked,
    _is_valid_feishu_id,
    api_feishu_config_get,
    api_feishu_config_save,
)
from kiro_crew.dashboard.messaging_api.imessage_settings import (  # noqa: F401
    _clean_imessage_path,
    _imessage_config_save,
    _is_valid_imessage_handle,
    api_imessage_config_get,
)
from kiro_crew.dashboard.messaging_api.notifications import (  # noqa: F401
    api_notification_ack,
    api_notification_channel_settings,
    api_notification_channels,
    api_notification_delete,
    api_notification_unack,
    api_notifications,
    api_notifications_ack_all,
    api_notifications_clear,
)
from kiro_crew.dashboard.messaging_api.proactive_send import (  # noqa: F401
    _SLACK_ERRORS_WITH_UNKNOWN_OUTCOME,
    _SOURCE_NAME_MAX,
    _audit_send_message,
    _deliver_send_message_fallback,
    _post_send_message_to_slack,
    _proactive_source_line,
    _publishing_session_meta,
    _read_send_message,
    _redact_all,
    _resolve_session_link_url,
    _resolve_session_target,
    _sanitize_blocks,
    _send_message_response,
    _SendMessageBody,
    _SendMessageOutcome,
    _session_link_blocks,
    _slack_rate_limited,
    _slack_rejected_outright,
    _with_source_line,
    api_delete_message,
    api_update_message,
)
from kiro_crew.dashboard.messaging_api.run_control import (  # noqa: F401
    _log_panel_dismissal,
    _native_child_refusal,
    _queue_unreadable,
    _queued_lookup,
    _queued_not_started,
    _queued_run,
    _queued_run_payload,
    _queued_runs,
    _retry_failed_run,
    _spawn_scope_refusal,
    api_spawn_delete,
    api_spawn_lost,
    api_spawn_mark_collected,
    api_spawn_release,
    api_spawn_retry,
    api_spawn_steer,
    api_spawn_stop_all,
)
from kiro_crew.dashboard.messaging_api.run_views import (  # noqa: F401
    _apply_result_view,
    _awaiting_spawn_approval,
    _redact,
    _spawn_result_view,
    api_spawn_list,
    api_spawn_status,
)
from kiro_crew.dashboard.messaging_api.slack_settings import (  # noqa: F401
    _slack_config_save_locked,
    _validate_slack_token,
    api_slack_config_get,
    api_slack_config_save,
    api_slack_manifest_status,
)
from kiro_crew.dashboard.messaging_api.slack_workspace import (  # noqa: F401
    _missing_scope_message,
    api_slack_manifest,
    api_slack_pins,
    api_slack_profile,
    api_slack_reactions,
)
from kiro_crew.dashboard.messaging_api.spawn import (  # noqa: F401
    STOPPED_BEFORE_START_CODE,
    _continue_on_loop,
    _qualified_model_refusal,
    _requested_backend,
    _slot_for_parent,
    _spawn_on_loop,
    _spawn_request_memory_mode,
    _stopped_before_start,
    api_spawn,
    api_spawn_continue,
)
from kiro_crew.dashboard.messaging_api.teams_settings import (  # noqa: F401
    _is_valid_teams_principal,
    _teams_config_save,
    _validate_teams_app_credentials,
    api_teams_config_get,
    api_teams_config_save,
)
from kiro_crew.dashboard.messaging_api.telegram_settings import (  # noqa: F401
    _telegram_config_save_locked,
    _validate_telegram_token,
    api_telegram_config_get,
    api_telegram_config_save,
)
from kiro_crew.dashboard.messaging_api.webex_settings import (  # noqa: F401
    _coerce_like,
    _is_valid_webex_email,
    _validate_webex_token,
    _webex_config_save,
    api_webex_config_get,
    api_webex_config_save,
)
from kiro_crew.dashboard.messaging_api.wecom_settings import (  # noqa: F401
    _is_valid_wecom_userid,
    _wecom_config_save_locked,
    api_wecom_config_get,
    api_wecom_config_save,
)
from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request  # noqa: F401
from kiro_crew.dashboard.state import (  # noqa: F401
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
    DashboardState,
    load_window_off_loop,
)
from kiro_crew.dashboard.token_auth import (  # noqa: F401
    LINK_WINDOW_SECS,
    caller_names_a_missing_slot,
    generate_token,
)
from kiro_crew.dashboard.ws_event_scope import (  # noqa: F401
    _audit_allow,
    _audit_deny,
    persisted_replay_denial_reason,
    persisted_snapshot_denial_reason,
    slot_owner_snapshot,
)
from kiro_crew.hidden_leaf_staging import EnvStagingRefused, publish_env  # noqa: F401
from kiro_crew.members import attributable_session_key, member_identity_for_session  # noqa: F401
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink  # noqa: F401
from kiro_crew.messaging.renderer import (  # noqa: F401
    chunk_for_transport,
    chunk_text,
    display_safe_for,
    format_overflow,
    render_options_as_text,
)
from kiro_crew.messaging.transport import (  # noqa: F401
    DM_TARGET_PREFIX,
    delivery_confirmed,
    sole_direct_target,
)
from kiro_crew.notifications.bus import (  # noqa: F401
    NotificationPayload,
    NotificationValidationError,
)
from kiro_crew.platform.context import redact_via_context  # noqa: F401
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY  # noqa: F401
from kiro_crew.platform_compat import IS_MACOS  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.slack.client import (  # noqa: F401
    BLOCKS_REMOTE_MEDIA_ERROR,
    blocks_request_remote_media,
)
from kiro_crew.slack.format import (  # noqa: F401
    SESSION_LINK_ACTION,
    build_options_blocks,
    extract_options,
)
from kiro_crew.slack.outbound import OPTIONS_FALLBACK_TEXT, PostedOptions  # noqa: F401
from kiro_crew.spawn_warm import warm_project_agents_for_spawn  # noqa: F401
from kiro_crew.subagent import (  # noqa: F401
    DEFERRED_QUEUED_REASONS,
    PINNED_RUNTIME_NOTE,
    SUCCESSOR_UNKNOWN,
    effort_applied_note,
    effort_drop_reason,
    effort_pending_note,
    parent_template_allowlists,
)
from kiro_crew.subagent_manager.admission.types import (  # noqa: F401
    QueuedReadUnavailable,
    QueuedRun,
    QueuedRunListing,
)
from kiro_crew.subagent_persistence import (  # noqa: F401
    DISMISSAL_FAILED,
    DISMISSAL_NO_FOLDER,
    PanelRecords,
    _agent_dir,
    classify_persisted_ending,
    read_panel_records,
    read_state,
    read_tombstone,
    record_panel_dismissal_outcome,
)
from kiro_crew.subagent_wait_reasons import stopped_before_start  # noqa: F401
from kiro_crew.validation import (  # noqa: F401
    _EMOJI_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_SESSION_RE,
    SLACK_THREAD_TS_RE,
    SPAWN_RUN_SCHEMA,
    ValidationError,
    validate_tool_args,
)

#: Seconds to wait for Slack when verifying a pasted token at save time.
_TOKEN_VERIFY_TIMEOUT = 8

#: A Slack message timestamp is `<10-digit epoch>.<6-digit sequence>` -- 17
#: characters. The cap is what BOUNDS the value: these routes forward `ts` to
#: Slack and write it into a SEL audit line, and the allowlist check that
#: rejects an untracked channel runs AFTER that line is written.
_SLACK_TS_MAX_LEN = 30


def _is_slack_ts(value: object) -> bool:
    """True for a Slack message timestamp that is safe to forward.

    Uses the shared ``SLACK_THREAD_TS_RE`` rather than an inline
    ``^\\d+\\.\\d+$``: ``\\d`` is Unicode-aware, so a string of Arabic-Indic
    numerals satisfied the old check and was forwarded verbatim. The shared
    pattern spells the class ``[0-9]`` to avoid precisely that.
    """
    return (
        isinstance(value, str)
        and len(value) <= _SLACK_TS_MAX_LEN
        and bool(SLACK_THREAD_TS_RE.match(value))
    )


#: Public field name -> .env credential key for the two Slack secrets.
_SLACK_SECRET_FIELDS = {
    "bot_token": "SLACK_BOT_TOKEN",
    "app_token": "SLACK_APP_TOKEN",
}

#: Transports ``send_message``'s ``channel_type`` may name, which is also the set
#: its channel ``session`` values may name. Reads the shared
#: ``CHANNEL_SEND_NAMESPACES`` rather than subtracting the two non-targets here:
#: a subtraction spelled at each reader drifts, and a drifted copy can leave a
#: Webex owner DM unreachable while this module's own leg already serves it.
#: The exclusions and their reasons are documented at the
#: definition — ``slack`` has its own client and streaming path and is deliberately
#: absent from ``state.channel_transports`` (``session="slack"`` is its spelling),
#: and ``unified`` is a session-key bucket rather than a transport.
_SEND_MESSAGE_CHANNEL_TYPES: frozenset[str] = frozenset(CHANNEL_SEND_NAMESPACES)
logger = logging.getLogger(__name__)


class _ThresholdPairInverted(Exception):
    """Raised from a ``_LockedSectionWrite`` finalizer: the merged section would
    store a soft threshold above its hard one. Aborts the write."""


class _CredentialTupleChanged(Exception):
    """Raised from the Teams finalizer: the (app id, password, tenant) about to be
    STORED is not the tuple Azure verified. Aborts the write."""


#: ``_LockedSectionWrite._prior`` when the section key was not in the document
#: at all -- distinct from a present ``null`` or other non-object value, which
#: a rollback has to put back rather than delete.
_ABSENT: Any = object()


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Subagents ──

#: Generic ``code`` for a spawn rejection that mints no identifier of its own.
#: Spelled once for the two handlers that answer with it (``api_spawn`` and
#: ``api_spawn_continue``), so the pair cannot drift apart. A rejection a client
#: acts on differently gets its OWN code at the decision instead -- see
#: ``subagent.AGENT_NOT_FOUND_CODE``.
_SPAWN_REJECTED_CODE = "spawn_rejected"

#: Wire text for a run control that reached the gateway with no session identity.
#: Actionable on purpose: the MCP wrapper hands this string to the model verbatim,
#: and "not found" alone would send the caller looking for a typo in the run id.
_IDENTITY_LESS_RUN_CONTROL = (
    "not found: run controls are scoped to the session that started the run, and "
    "this call carried no session identity (X-Session-Key). A kiro-cli process that "
    "multiplexes sessions cannot name one; see the strict-identity diagnosis in "
    "`kirocrew doctor` (mcp_gateway.stub_servers)."
)


def _run_belongs_to_caller(caller: str, run_id: str, parent: object) -> bool:
    """Whether *caller* may control run *run_id* whose originating session is *parent*.

    Ownership is the ONLY admission: the run's parent session, or the run itself.
    A caller with no identity owns nothing that a session started -- it is admitted
    to a run with no parent (one the host operator started from the CLI, which
    carries the internal secret and no session) and to nothing else. Neither the
    caller's memory store nor the transport it arrived on widens this.
    """
    if caller == f"subagent:{run_id}":
        return True
    if parent is None:
        # No record of this run at all: nothing vouches for who started it, so
        # nobody owns it. Reading "unknown" as "parentless" would let a caller
        # with no identity act on any id it can name.
        return False
    parent_key = parent if isinstance(parent, str) else ""
    return parent_key == caller


def parent_work_supported(state: Any, parent_session: str) -> bool:
    """Only dashboard-owned turns have the verified busy-turn completion queue.

    The spawn receipt tells the parent whether it may do a short, bounded step
    of its own non-overlapping work before ending its turn, or must yield at
    once. Channel-only, nested and background callers retain their yield
    boundary. A channel linked to a dashboard slot uses the same queue as
    dashboard chat.
    """
    if not parent_session or parent_session.startswith(("subagent:", "cron:", "hook:")):
        return False
    slots = getattr(state, "_slots", None)
    return isinstance(slots, dict) and any(
        effective_session_key(slot) == parent_session for slot in slots.values()
    )


#: Bounds on the inline-collected ids a slot retains: each id's length (run ids
#: are 16 hex characters), and the set as a whole, since only a completion that
#: matches an id evicts it.
_COLLECTED_ID_MAX_LEN = 128
_COLLECTED_IDS_CAP = 1000


_SPAWN_STATUS_MAX_LINES = 2000  # cap lines returned per spawn_status page
_SPAWN_STATUS_MAX_GREP_LEN = 500


#: No unit holds the child, so no crew-log record was owed or written.
DISMISSAL_LOG_ABSENT = "absent"
#: A unit holds the child and the append COMMITTED.
DISMISSAL_LOG_COMMITTED = "committed"
#: A unit holds the child and the append did not commit inside the bound. The run
#: exists, so this is not an unknown id; the dismissal simply did not happen.
DISMISSAL_LOG_FAILED = "failed"


# ── Sessions / Notifications ──


_MAX_BLOCKS = 50  # Slack Block Kit limit
_MAX_WALK_DEPTH = 10  # defense-in-depth against deeply nested LLM output

#: The Block Kit media boundary is defined and enforced at the Slack client seam
#: (``slack/client.py``), which every outbound tree passes through. This entry
#: keeps its own 400 with a machine-readable code for agent callers, so it asks
#: that one predicate instead of restating the rule in a second place.
_blocks_request_remote_media = blocks_request_remote_media


#: ``session`` values on ``/api/send-message`` that name a delivery MODE rather
#: than a chat channel. Any other value is looked up in the registered channel
#: transports, so a channel becomes reachable here by registering its transport
#: instead of by editing this module. Slack is reserved because it is delivered
#: by its own client, which is not in ``channel_transports``.
_RESERVED_SESSION_TARGETS = frozenset({"origin", SLACK_NAMESPACE})

#: The shape every registered ``channel_type`` has. A ``session`` value that does
#: not match is not looked up as a channel at all: the value is agent-authored and
#: reaches an error body and the SEL ``resources`` field, so bounding its length
#: and alphabet here keeps an unrecognized value from carrying newlines or
#: kilobytes into the audit trail. It still degrades to the dashboard
#: notification, which is what an unknown ``session`` has always done.
_CHANNEL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: The ``configured_targets()`` prefix every transport gives a DIRECT
#: conversation (``user:<identity>``). A ``thread:`` or room target is a
#: different audience and is never the owner's DM. The one spelling lives in
#: ``messaging.transport``, beside the owner inference that reads it.
_DM_TARGET_PREFIX = DM_TARGET_PREFIX

#: Request fields that only exist in Slack's protocol. Combined with a channel
#: ``session`` they are refused rather than dropped: a caller that asked for a
#: threaded reply or a named Slack channel cannot observe the drop, and would
#: read a private DM as a successful post to a shared channel.
_SLACK_ONLY_BODY_FIELDS = (
    "channel",
    "user",
    "blocks",
    "thread_ts",
    "reply_broadcast",
    "unfurl_links",
    "unfurl_media",
)


#: ``action_id`` on the "Open session" link button. Slack still emits a
#: ``block_actions`` event when a URL button is clicked, so this stable id is
#: declared and ack'd as a no-op by ``slack.interactions.dispatch``; the ``url``
#: is what actually opens the tab. The id lives in ``slack.format`` so the
#: producer here and the router there share one source of truth.


# Slack rejects a ``section`` block whose mrkdwn ``text`` exceeds 3000 chars, so
# a plain-text send longer than this is posted as text (via ``post_message``)
# with the button trailing, rather than merged into one ``section`` Slack refuses.
_SLACK_SECTION_TEXT_MAX = 3000


async def api_send_message(request: web.Request) -> web.Response:
    """POST /api/send-message — send a message to a chat surface and/or dashboard.

    ``session`` picks the surface: ``"origin"`` injects into the dashboard session
    that spawned the calling cron, ``"slack"`` adds a Slack DM, and any other
    value names a registered channel transport (``"discord"``) and delivers a DM
    to that channel's configured owner. The Slack-only options are refused, not
    ignored, when combined with a channel session (see ``_SLACK_ONLY_BODY_FIELDS``).

    ``_read_send_message`` reads and refuses the body (and delivers a configured
    ``channel_type`` + ``target_id`` itself), and the fallback legs, the audit row
    and the response are built in ``messaging_api.proactive_send``. The redaction and authorization of the
    Slack target and the origin-session injection stay here.
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811
    from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel  # noqa: F811
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    state: DashboardState = request.app["state"]
    send = await _read_send_message(request, state)
    if isinstance(send, web.Response):
        return send
    body, text, title, blocks = send.body, send.text, send.title, send.blocks
    target_channel, target_user = send.target_channel, send.target_user
    thread_ts, reply_broadcast = send.thread_ts, send.reply_broadcast
    session_name = send.session_name
    channel_target, channel_type = send.channel_target, send.channel_type

    # Validate format first, then redact (#2)
    if target_channel and (
        len(target_channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(target_channel)
    ):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    if target_user and not USER_ID_RE.match(target_user):
        return web.json_response({"error": "invalid user ID format"}, status=400)

    # Redact after format validation
    if target_channel:
        target_channel, _ = redact_exfiltration_urls(target_channel)
        target_channel, _ = redact_credentials(target_channel)
    if target_user:
        target_user, _ = redact_exfiltration_urls(target_user)
        target_user, _ = redact_credentials(target_user)

    # Sanitize LLM-generated content before any external surface.
    # This covers all downstream paths (session injection, fallback, Slack,
    # and every channel transport), which is why the DISPLAY-form floor belongs
    # here rather than at each delivery site: a proactive body is posted as-is by
    # the channel legs below, so the literal-form scan alone let a
    # markdown-collapse credential (`AKIA**IOSFODNN7EXAMPLE**`, which the client
    # renders whole) through on a path that never passes a renderer -- the one
    # place every turn egress applies this floor.
    text, _ = redact_for_display(text, _redact_all)
    title, _ = redact_for_display(title, _redact_all)
    if blocks:
        blocks = _sanitize_blocks(
            blocks, redact_exfiltration_urls, redact_credentials, display_form=True
        )

    # render [OPTIONS: ...] tags as interactive buttons on the
    # plain-text path (when the caller did not supply explicit blocks — those
    # own their own layout). Strip the tag from the text used for both the
    # dashboard notification and the Slack post; an actions block is appended
    # after the message when options are present.
    options: list[str] = []
    if not blocks:
        text, options = extract_options(text)

    # --- Authorization gates (before any side effects) ---
    if target_channel and not is_tracked_channel(target_channel):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="send_message",
            outcome="denied",
            downstream_service="slack",
            resources=f"target_channel={target_channel}",
        )
        return web.json_response(
            {
                "error": f"channel {target_channel} not in tracked channels. "
                "Add it to config.json: "
                f'{{"slack": {{"tracking_channels": [{{"channel_id": "{target_channel}"}}]}}}}. '
                "Then restart the gateway."
            },
            status=403,
        )

    if target_user and not is_allowed_user(target_user):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="send_message",
            outcome="denied",
            downstream_service="slack",
            resources=f"target_user={target_user}",
        )
        return web.json_response(
            {
                "error": "user not in allowlist — configure allowed_users in config.json",
                "code": "user_not_in_allowlist",
            },
            status=403,
        )

    sent_session = False
    # A channel target is not a session-injection target: _resolve_session_target
    # accepts only "origin", and the delivery below keys off channel_target.
    target_session = "" if channel_target else session_name
    job_name = None
    # What the fallback legs reached, on one record the audit below reads even
    # when a leg raised part-way.
    outcome = _SendMessageOutcome()
    try:
        # ───────────────────────────────────────────────────────────────────
        # send_message delivery contract
        # ───────────────────────────────────────────────────────────────────
        # For cron jobs, the intended behavior is:
        #
        #   1. Try the origin dashboard session first (the chat that created
        #      this cron). Inject the message there so the session agent can
        #      react to it (not just display it). When injection succeeds,
        #      the message appears in the chat UI directly — no extra bell
        #      notification needed.
        #   2. Fall through to owner Slack DM if origin is unreachable.
        #   3. Dashboard notification (bell icon + notifications.jsonl) fires
        #      ONLY on the fallback path, so no-Slack setups still surface
        #      messages that couldn't reach their origin. The invariant is
        #      "never silently dropped", not "always notified".
        #
        # "Origin reachable" = one of:
        #   - Hot: slot in state._slots (user has the tab open) → fast path
        #   - Cold: slot not loaded but JSONL exists without closed=true →
        #     rehydrate_slot_from_history_async restores it from disk, tab reappears
        #
        # "Origin unreachable" = any of:
        #   - User clicked ✕ on the tab (closed=true in JSONL metadata) —
        #     respect the close, do NOT resurrect the tab
        #   - JSONL file deleted entirely (history.delete_session)
        #   - Cron created from dashboard UI without an originating chat
        #     (job.session_key is empty — api_crons_create never sets it)
        #   - Cron's caller_session doesn't match any known job
        #
        # session param values (enforced by _resolve_session_target):
        #   - "origin": route to originating dashboard session
        #   - "slack":  Slack DM + notification
        #   - omitted:  dashboard notification only (default)
        # ───────────────────────────────────────────────────────────────────
        # B: cron-originated sends deliver to the owner Slack DM by default —
        # the documented "cron → Slack DM + dashboard" behavior — even on a
        # bare send with no explicit session/channel/user. For session=origin
        # this only takes effect as the fallback when the origin slot is
        # unreachable (see the contract above). Non-cron bare sends remain
        # dashboard-notification-only.
        caller_session = body.get("caller_session", "")
        # The header, not the body, is what identifies a non-cron caller to the
        # channel path: token_auth kernel-attests it against the AF_UNIX peer's
        # own process ancestry. See _channel_delivery_key.
        declared_session = request.headers.get("X-Session-Key", "")
        # Validate the cron session format before trusting it to escalate
        # routing from notification-only to owner Slack DM — a malformed or
        # injected value must not abuse that upgrade.
        is_cron_caller = bool(CRON_SESSION_RE.match(caller_session))
        # A channel session takes the routing over, INCLUDING the cron default:
        # a cron asking for a Discord DM did not ask for a Slack one as well.
        send_to_slack = not channel_target and (
            target_session == "slack" or bool(target_channel) or bool(target_user) or is_cron_caller
        )
        # A channel_type send names ONE destination, so Slack is not its
        # fallback: a failed channel delivery falling through to the owner DM
        # would post the message to an audience the caller never named, and the
        # 502 below is what tells the caller nothing was delivered. This is also
        # why the MCP tool vets ONLY channel_type's transport under the
        # ``channels`` scope — Slack is not a destination of such a call.
        if channel_type:
            send_to_slack = False
        if target_session == "slack":
            target_session = ""
        if target_session:
            slot_key, job_name = _resolve_session_target(state, target_session, caller_session)
            if slot_key:
                # Resolve the origin slot. get_slot is the hot path (fast,
                # O(1) dict lookup). On miss, rehydrate_slot_from_history_async
                # restores from disk if the session exists and isn't closed,
                # with the transcript read on a worker thread so a large store
                # does not stall the loop for every other request.
                # Truly-gone sessions (never persisted, deleted, or closed)
                # return None and delivery falls through to the Slack DM
                # path below — no phantom empty tab is ever created.
                slot = state.get_slot(slot_key)
                was_loaded = slot is not None
                if slot is None:
                    slot = await rehydrate_slot_from_history_async(state, slot_key)
                elif getattr(slot, "window_pending", False) is True:
                    # A tab restored as a sidebar row loads its window before
                    # the delivery below appends to it, with the read off the loop.
                    await load_window_off_loop(slot)
                    slot = state.get_slot(slot_key)
                logger.info(
                    "send_message session=origin resolved slot_key=%s job=%s was_loaded=%s rehydrated=%s",
                    slot_key,
                    job_name,
                    was_loaded,
                    (slot is not None and not was_loaded),
                )
                if slot:
                    label = job_name or "cron"
                    label, _ = redact_exfiltration_urls(label)
                    label, _ = redact_credentials(label)
                    # text and title already redacted above
                    # Text wrapper kept for LLM context and queue detection;
                    # cronLabel in cls JSON provides structured data for frontend.
                    wrapped = f'{CRON_NOTIFY_PREFIX}"{label}"]\n{text}\n{CRON_NOTIFY_END}'
                    # The session decides idle (a cron row and a turn) vs running (a
                    # queued card, dropping the oldest entry when the live queue is full).
                    from kiro_crew.dashboard import session_deliver

                    delivery = await session_deliver.deliver(
                        state, slot, wrapped, session_deliver.cron(label), evict_oldest=True
                    )
                    state.push_slots_update()
                    # A full queue with nothing that may give way wrote nothing,
                    # so the message takes the fallback below instead.
                    sent_session = delivery != session_deliver.QUEUE_FULL
        # Fall back to normal delivery if no session target or session is gone
        if not sent_session:
            await _deliver_send_message_fallback(
                state,
                body,
                outcome,
                text=text,
                title=title,
                blocks=blocks,
                options=options,
                target_channel=target_channel,
                target_user=target_user,
                thread_ts=thread_ts,
                reply_broadcast=reply_broadcast,
                target_session=target_session,
                job_name=job_name,
                channel_target=channel_target,
                channel_type=channel_type,
                caller_session=caller_session,
                declared_session=declared_session,
                attribution_session=await asyncio.to_thread(attributable_session_key, request),
                is_cron_caller=is_cron_caller,
                send_to_slack=send_to_slack,
            )
    finally:
        _audit_send_message(
            outcome,
            sent_session=sent_session,
            target_channel=target_channel,
            target_user=target_user,
            thread_ts=thread_ts,
            reply_broadcast=reply_broadcast,
            channel_target=channel_target,
            channel_type=channel_type,
        )
    return _send_message_response(
        outcome,
        sent_session=sent_session,
        channel_target=channel_target,
        channel_type=channel_type,
    )


async def _write_env_off_loop(updates: dict[str, str | None]) -> None:
    """Run the blocking ``.env`` write on a worker, drained under the config lock.

    Every caller holds ``_get_config_lock()`` across this, and a thread cannot be
    cancelled -- so a bare ``await asyncio.to_thread(...)`` lets a cancelled
    request (a client disconnecting mid-save, gateway shutdown) unwind the
    ``async with`` while the worker is still rewriting ``.env``. The next channel
    save then enters the critical section against a file still being replaced and
    writes it back from lines it read before the first write landed, discarding
    whichever credential the other save was persisting.

    Shielding and draining puts the lock release after the worker instead. It
    cannot change WHETHER the write happens -- the thread runs to completion
    either way -- so the only thing it decides is whether the lock outlives it.
    The cancellation is re-raised, never swallowed.

    All six channel saves go through here. The offload itself is already in
    place on every one of them; the bare offload is what leaves the hole, so
    covering a subset would leave the same window open in the rest.

    A ``.env`` saved as UTF-16 or UTF-32 is refused before anything is written
    (:func:`_write_env_updates_locked` never overwrites a file it cannot
    parse), and so is one whose masked staging directory cannot be used
    (:class:`~kiro_crew.hidden_leaf_staging.EnvStagingRefused`). Each refusal is
    raised here as a 409 carrying the fix, so every channel save answers it the
    same way instead of with an opaque 500. Every
    saver (Slack, Teams, Webex, WeCom, Feishu, Discord and Telegram) reaches
    this through :func:`_write_env_or_roll_back`, which rolls the config write
    back on any failure, so on every channel the 409 means nothing was saved.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(_write_env_updates, updates))
    try:
        await asyncio.shield(fut)
    except asyncio.CancelledError:
        await asyncio.wait([fut])
        raise
    except _loader.EnvFileWideEncodingError as exc:
        raise _wide_env_refusal(exc) from exc
    except EnvStagingRefused as exc:
        raise web.HTTPConflict(
            text=json.dumps({"error": str(exc)}),
            content_type="application/json",
        ) from exc


# ── Discord configuration API: dashboard/messaging_api/discord_settings.py ──

#: Loose shape check for Discord bot tokens: three dot-separated base64url
#: segments (e.g. "MTA5...aBc.GhIjKl.MnOpQrStUvWxYz0123456789_-").
_DISCORD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}$")


# ── Telegram configuration API: dashboard/messaging_api/telegram_settings.py ──

#: Loose shape check for Telegram bot tokens: "<bot_id>:<secret>" from
#: @BotFather (e.g. "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw").
_TELEGRAM_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{10,}$")


# ── Webex configuration API: dashboard/messaging_api/webex_settings.py ──

#: Seconds to wait for Webex when verifying a pasted token at save time.
_WEBEX_VERIFY_TIMEOUT = 8


async def api_teams_activity(request: web.Request) -> web.Response:
    """POST /api/messaging/teams — Bot Framework inbound webhook (late-bound).

    The route is registered at app-build time (aiohttp freezes routes at
    startup), but the handler that validates the JWT + drives the turn is the
    ``TeamsClient.on_activity`` built by ``maybe_start_teams`` once credentials
    are present. Until then (channel disabled/uncredentialed) we return 503.

    This route is exempt from the dashboard cookie gate and from the CSRF Origin
    check, for POST only (``token_auth._BYPASS_EXACT_METHODS`` /
    ``CSRF_EXEMPT_EXACT_METHODS``); the delegated handler performs Bot Framework
    JWT validation itself before doing anything with the payload. Both
    exemptions make this the one route in the product an unauthenticated caller
    can reach, so the two hardening steps below run before delegating.
    """
    # Deferred imports: Teams is an optional, default-off channel, and importing
    # its client also probes for the optional PyJWT. Neither belongs on the
    # gateway boot path, which imports this module.
    from kiro_crew import webhooks  # noqa: F811
    from kiro_crew.teams.client import (  # noqa: F811
        TEAMS_ACTIVITY_REQUEST_KEY,
        TEAMS_MAX_ACTIVITY_BYTES,
    )

    state: DashboardState = request.app["state"]
    handler = getattr(state, "teams_on_activity", None)
    if handler is None:
        return web.Response(status=503, text="Teams channel not enabled")

    # Failed-auth throttle, the same per-source abuse damper /api/hooks/agent
    # uses. An anonymous POST here is free to the sender but not to us: an
    # unknown JWT ``kid`` sends the validator to the Bot Framework JWKS endpoint,
    # so a flood buys a remote fetch plus an audit write per request. Slowed
    # rather than answered at line rate; the real boundary remains the JWT.
    #
    # 429 rather than the 200 the in-flight shed uses: a rate limit is exactly
    # what the Connector's retry-with-backoff is for.
    #
    # SKIPPED when a proxy terminated the connection, and that is the whole point
    # of the check rather than a convenience. ``request.remote`` is then the
    # proxy's address for EVERY caller, so the real Connector and an anonymous
    # flood share one counter -- and in two of the three topologies
    # ``docs/teams-integration.md`` documents (a reverse proxy, a dev tunnel) that
    # is the normal deployment. Throttling there converts ~10 bogus POSTs into a
    # 300s outage for the channel, renewably, because a 429 is never an accepted
    # activity and so never clears the count. Trusting a forwarded header instead
    # would make a spoofable field the identity, which is its own decision;
    # bounding only the direct-bind topology is the honest half.
    source = request.remote or "unknown"
    throttled_source = "" if is_proxied_request(request) else source
    if throttled_source and webhooks.auth_throttle_blocked(throttled_source):
        # A bare enqueue: SEL is warmed at gateway startup
        # (sel.warm_sel_singleton), so even when this route is the
        # first request a fresh gateway serves, no construction runs here.
        _sel().log_api_access(
            caller=source,
            operation="teams.activity",
            outcome="denied",
            source="teams",
            error="auth failures throttled",
        )
        return web.json_response(
            {"error": "too many failed attempts", "code": "auth_throttled"}, status=429
        )

    # Bounded body read. It happens HERE, not in the client, for two reasons: the
    # cap is a property of the exposed route, and ``teams/client.py`` keeps its
    # no-dashboard-imports property. ``on_activity`` reads the parsed dict from
    # the request mapping, so the body is parsed exactly once and never past the
    # cap.
    #
    # ``require_json_content_type=False`` is what KEEPS that true here, and is not
    # a relaxation of this route's perimeter. The shared helper's 415 returns
    # before a single byte is read; the ``status == 413`` filter below then drops
    # it, because a verdict derived from body CONTENT must not precede the JWT
    # check. The body would therefore reach ``on_activity`` unstashed and be
    # re-parsed by its bare ``request.json()`` fallback on a stream nobody has
    # read -- bounded only by the app-wide ``client_max_size``, not by
    # ``TEAMS_MAX_ACTIVITY_BYTES``. Opting out means the capped read runs, so an
    # over-cap activity is still refused 413 whatever media type it declared.
    # Whether to REFUSE a non-JSON media type from the Connector is a separate
    # decision about an external contract; see ``read_bounded_json``.
    body, cap_error = await read_bounded_json(
        request,
        max_bytes=TEAMS_MAX_ACTIVITY_BYTES,
        require_json_content_type=False,
    )
    if cap_error is not None and cap_error.status == 413:
        return cap_error
    if body is not None:
        request[TEAMS_ACTIVITY_REQUEST_KEY] = body
    # A body that is not a JSON object is deliberately NOT answered here. The
    # ordering guarantee is that the token check precedes any verdict derived
    # from body CONTENT, so the stash is left unset and ``on_activity`` answers
    # 400 after its JWT gate. An over-cap body is different: refusing on SIZE
    # reveals nothing about the payload and must happen before any buffering.

    response = await handler(request)
    # 401 is ``on_activity``'s only authentication refusal (invalid or
    # unverifiable bearer); anything it accepts clears the source, exactly as the
    # hooks webhook does after a valid token authenticates. Bookkeeping is skipped
    # for a proxied request for the same reason the check above is: the counter
    # would be shared across every caller.
    if throttled_source:
        if response.status == 401:
            webhooks.record_auth_failure(throttled_source)
        elif response.status < 400:
            webhooks.record_auth_success(throttled_source)
    return response


#: Azure AD OAuth error codes that mean "these credentials are wrong" rather than
#: "Azure is having trouble". Anything else is treated as unverifiable.
_TEAMS_CREDENTIAL_REJECT_STATUSES = frozenset({400, 401, 403})


async def api_imessage_config_save(request: web.Request) -> web.Response:
    """PUT /api/imessage/config — persist the iMessage config (config.json)."""
    # A save is a config.json + credential transaction: a cancelled request
    # (client gone, gateway shutting down) must not abandon it between its
    # phases -- see ``run_to_completion``.
    return await run_to_completion(_imessage_config_save(request))


# Channels whose SDK ships as an optional extra rather than in core. Maps the
# channel to (import name, extra name) so the panel can report BOTH whether the
# SDK is importable by this gateway process and the exact command that installs
# it into this interpreter. Every panel that reports these renders the shared
# optional-SDK card instead of a bare ``pip install`` line.
_CHANNEL_SDK_EXTRA: dict[str, tuple[str, str]] = {
    "feishu": ("lark_oapi", "feishu"),
    # PyJWT installs as the ``jwt`` package (teams/client.py imports ``jwt``).
    "teams": ("jwt", "teams"),
    # whatsapp/client.py probes ``find_spec("neonize")``.
    "whatsapp": ("neonize", "whatsapp"),
}


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.handlers.messaging.<name>`` reaches it wherever it lives;
# see ``kiro_crew.dashboard.messaging_api``. Run once, after this body has bound
# every name and before the owner-surface guard below wraps the spawn routes.
_facade_compose(
    globals(),
    (
        _owner_spawn,
        _owner_run_control,
        _owner_run_views,
        _owner_notifications,
        _owner_proactive_send,
        _owner_channel_delivery,
        _owner_slack_settings,
        _owner_discord_settings,
        _owner_telegram_settings,
        _owner_teams_settings,
        _owner_webex_settings,
        _owner_imessage_settings,
        _owner_wecom_settings,
        _owner_feishu_settings,
        _owner_browser_routes,
        _owner_channel_config_writes,
        _owner_slack_workspace,
        _owner_channel_folder_backfill,
        _owner_agent_push,
    ),
)


# A private member reaches only the runs its own memory store owns: the per-run
# routes run behind ``_spawn_scope_refusal``, the listed routes scope their own
# caller in their body, and any other ``api_spawn*`` handler is refused to
# private members until it is listed here on purpose.
guard_owner_surface_routes(
    globals(),
    prefix="api_spawn",
    member_scoped=frozenset(
        {
            "api_spawn",
            "api_spawn_continue",
            "api_spawn_lost",
            "api_spawn_mark_collected",
            "api_spawn_list",
            "api_spawn_stop_all",
        }
    ),
    resource_scoped={
        name: _spawn_scope_refusal
        for name in (
            "api_spawn_steer",
            "api_spawn_release",
            "api_spawn_status",
            "api_spawn_retry",
            "api_spawn_delete",
        )
    },
)
