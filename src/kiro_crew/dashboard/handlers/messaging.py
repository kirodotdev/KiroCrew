"""Messaging handlers — spawn, notifications, send-message, slack profile.

The route families are composed from the owners in ``kiro_crew.dashboard.messaging_api``;
this module stays their import path and their patch surface (see that package).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util  # noqa: F401
import inspect  # noqa: F401
import json
import logging
import math  # noqa: F401
import os
import platform
import re
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, Callable, cast  # noqa: F401

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.agent_sdk.drivers.acp_vocab import NATIVE_CHILD_NOT_RESUMABLE  # noqa: F401
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser.command_bus import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_DRAIN_WAIT_MS,
    NoPanelError,
    QueueFullError,
    get_command_bus,
)
from kiro_crew.browser_cli import install as browser_cli_install
from kiro_crew.browser_cli import install_job as browser_install_job
from kiro_crew.browser_cli import launcher as browser_cli_launcher
from kiro_crew.browser_cli import token as browser_cli_token
from kiro_crew.browser_cli import view as browser_cli_view
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
from kiro_crew.dashboard import messaging_api as _messaging_api
from kiro_crew.dashboard.channel_folders import (  # noqa: F401
    CHANNEL_CONFIG_SECTIONS,
    channel_restart_required,
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.channel_slots import backfill_channel_folder
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
    subagent_event_slot,
)
from kiro_crew.dashboard.handlers._shared import (  # noqa: F401
    _pip_install_channel_available,
    guard_owner_surface_routes,
    internal_memory_scope,
    pip_extra_install_command,
    read_bounded_json,
)
from kiro_crew.dashboard.handlers.browser_view_relay import ROUTE_PREFIX
from kiro_crew.dashboard.handlers.core import _hot_apply_after_write  # noqa: F401
from kiro_crew.dashboard.messaging_api import channel_delivery as _owner_channel_delivery
from kiro_crew.dashboard.messaging_api import discord_settings as _owner_discord_settings
from kiro_crew.dashboard.messaging_api import feishu_settings as _owner_feishu_settings
from kiro_crew.dashboard.messaging_api import imessage_settings as _owner_imessage_settings
from kiro_crew.dashboard.messaging_api import notifications as _owner_notifications
from kiro_crew.dashboard.messaging_api import proactive_send as _owner_proactive_send
from kiro_crew.dashboard.messaging_api import run_control as _owner_run_control
from kiro_crew.dashboard.messaging_api import run_views as _owner_run_views
from kiro_crew.dashboard.messaging_api import slack_settings as _owner_slack_settings
from kiro_crew.dashboard.messaging_api import spawn as _owner_spawn
from kiro_crew.dashboard.messaging_api import teams_settings as _owner_teams_settings
from kiro_crew.dashboard.messaging_api import telegram_settings as _owner_telegram_settings
from kiro_crew.dashboard.messaging_api import webex_settings as _owner_webex_settings
from kiro_crew.dashboard.messaging_api import wecom_settings as _owner_wecom_settings
from kiro_crew.dashboard.messaging_api.channel_delivery import (  # noqa: F401
    _channel_delivery_key,
    _ChannelSendFailed,
    _deliver_channel_dm,
    _deliver_to_channel,
    _owner_dm_target,
    _send_to_channel_target,
    _vet_channel_send,
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
    _audit_send_message,
    _deliver_send_message_fallback,
    _post_send_message_to_slack,
    _read_send_message,
    _redact_all,
    _resolve_session_link_url,
    _resolve_session_target,
    _sanitize_blocks,
    _send_message_response,
    _SendMessageBody,
    _SendMessageOutcome,
    _session_link_blocks,
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
    _remote_run_operation_refusal,
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
)
from kiro_crew.dashboard.messaging_api.spawn import (  # noqa: F401
    _continue_on_loop,
    _floored_caller_refusal,
    _slot_for_parent,
    _spawn_on_loop,
    _spawn_request_memory_mode,
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
from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request
from kiro_crew.dashboard.state import (  # noqa: F401
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    PERSISTED_SUBAGENT_REPLAY_KEEP,
    PERSISTED_SUBAGENT_REPLAY_MAX_AGE_SECS,
    DashboardState,
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
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink  # noqa: F401
from kiro_crew.messaging.renderer import (  # noqa: F401
    chunk_for_transport,
    chunk_text,
    display_safe_for,
    format_overflow,
)
from kiro_crew.messaging.transport import (  # noqa: F401
    DM_TARGET_PREFIX,
    delivery_confirmed,
    sole_direct_target,
)
from kiro_crew.notifications.bus import (
    NotificationPayload,
    NotificationValidationError,
)
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
    SUCCESSOR_UNKNOWN,
    effort_applied_note,
    effort_drop_reason,
    parent_spawn_allowlists,
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


class _LockedSectionWrite:
    """One channel's ``config.json`` write, through ``update_config_locked``.

    Every per-channel settings saver in this module merges a staged dict into
    its own top-level section and, when the paired ``.env`` write then fails,
    undoes that merge. This is that step, written once, because each saver is a
    hand-copied credential-write skeleton and the copy is how an unlocked write
    arrives (see ``TestTheAtomicJsonWriteConfigFamilyIsRatcheted``).

    ``apply`` merges ``changes`` into -- and drops ``drop_keys`` from -- the named
    section of the config as re-read INSIDE the sidecar lock, so a concurrent
    edit to an unrelated key is preserved instead of being replaced by the
    caller's older snapshot. The advisory lock on ``<path>.lock`` is what keeps
    another PROCESS (``kirocrew config set``) from landing between the read and
    the write; the in-process ``_get_config_lock()`` the caller holds only
    serializes writers inside this one.

    ``restore`` undoes only the keys this write touched, and only where the
    stored value is still the one it wrote. Rewriting the file from a whole-file
    snapshot would revert whatever a concurrent writer landed; restoring the
    whole section would still discard a concurrent ``kirocrew config set
    <channel>.*`` that arrived between our write and the rollback. A key whose
    stored value differs from what we wrote has been changed by someone else
    since, and reverting it would destroy their edit to undo ours.

    ``seed_from`` names a legacy section (WeCom's ``wechat``) whose COPY becomes
    the starting point when the target section is absent, so an install still on
    the old key keeps its allow-list and thresholds across its first save instead
    of having them shadowed by a bare new section.

    ``drop_keys`` and ``blank_keys`` are the legacy plaintext-credential purge
    (``bot_token`` / ``app_password`` stored in ``config.json`` before the
    ``.env`` slot existed). They are decided against the document the write
    lands on, not the snapshot: a copy a concurrent writer landed after the
    handler's read is purged too, so clearing the ``.env`` credential cannot
    leave a fallback behind that a restart would authenticate with. ``drop``
    removes the key; ``blank`` sets it to ``""`` where present. Both are undone
    by ``restore``.

    ``finalize`` runs on the MERGED section, inside the lock, for a rule that
    couples the written keys to their stored counterparts (a soft/hard threshold
    pair): the pre-lock snapshot the handler validated against may not be the
    document the write lands on, so the coupled check has to be re-decided
    against the fresh one. It may adjust the section or raise; a raise aborts
    the write and propagates to the caller.

    Both run off the event loop through ``drained_to_thread``: the locked
    read-modify-write does file IO and may block on another process holding the
    lock, neither of which belongs on the loop, and a thread cannot be
    cancelled, so a cancelled request must not unwind (releasing
    ``_get_config_lock()`` and skipping its paired ``.env`` write) while the
    worker is still rewriting the file.
    """

    def __init__(
        self,
        path: Path,
        section: str,
        changes: dict[str, object],
        *,
        drop_keys: tuple[str, ...] = (),
        blank_keys: tuple[str, ...] = (),
        seed_from: str | None = None,
        finalize: Callable[[dict], None] | None = None,
    ) -> None:
        self._path = path
        self._section = section
        self._changes = dict(changes)
        self._drop_keys = drop_keys
        self._blank_keys = blank_keys
        self._seed_from = seed_from
        self._finalize = finalize
        # The values actually stored under the keys this write moved -- the
        # requested keys as ``finalize`` may have adjusted them, plus any other
        # key ``finalize`` changed; ``restore`` compares against these.
        self._written: dict[str, object] = dict(changes)
        # The pre-mutation section, captured inside ``apply`` because that is the
        # only point at which the pre-mutation state is known to be current: a
        # copy of the object, the raw non-object value that was stored under the
        # key, or ``_ABSENT`` when the key was not there.
        self._prior: Any = _ABSENT
        # What ``apply`` started a MISSING section from: the legacy copy, or ``{}``.
        # ``restore`` uses it to recognise a section that exists only because this
        # write created it, seeded contents included.
        self._created_from: dict | None = None

    def apply(self, fresh: dict) -> dict | None:
        """Merge into *fresh*; ``None`` when the section would be unchanged.

        ``None`` tells ``update_config_locked`` to skip the write, so a save
        whose every key already holds the requested value (or whose purge finds
        nothing to purge) rewrites nothing and wakes no watcher.
        """
        section = fresh.get(self._section)
        if self._section not in fresh:
            self._prior = _ABSENT
        else:
            self._prior = dict(section) if isinstance(section, dict) else section
        if not isinstance(section, dict):
            legacy = fresh.get(self._seed_from) if self._seed_from else None
            # A COPY: the legacy block is never mutated in place.
            section = dict(legacy) if isinstance(legacy, dict) else {}
            self._created_from = dict(section)
            fresh[self._section] = section
        for key in self._drop_keys:
            section.pop(key, None)
        for key in self._blank_keys:
            if key in section:
                section[key] = ""
        section.update(self._changes)
        touched = set(self._changes)
        if self._finalize is not None:
            pre = dict(section)
            self._finalize(section)
            # Every key ``finalize`` moved is ours to undo as well -- a soft
            # threshold it pulled down to a lowered hard one was never requested,
            # and a rollback that left it lowered would lose it silently.
            touched |= {
                k for k in set(pre) | set(section) if pre.get(k, _ABSENT) != section.get(k, _ABSENT)
            }
        self._written = {k: section[k] for k in touched if k in section}
        if isinstance(self._prior, dict) and section == self._prior:
            return None
        return fresh

    def restore(self, fresh: dict) -> dict:
        section = fresh.get(self._section)
        if not isinstance(section, dict):
            # Nothing of ours left to undo (the section is gone or was replaced
            # wholesale by another writer).
            return fresh
        # What each key held before we wrote it: the stored object, or -- for a
        # section we created -- the seed it was created from, so a key we wrote
        # OVER a seeded legacy value goes back to that value rather than away.
        if isinstance(self._prior, dict):
            before = self._prior
        else:
            before = self._created_from if self._created_from is not None else {}
        for key, written in self._written.items():
            if section.get(key) != written:
                continue  # not ours any more
            if key in before:
                section[key] = before[key]
            else:
                section.pop(key, None)
        for key in self._drop_keys:
            if key in section:
                continue  # someone re-set it since; theirs
            if key in before:
                section[key] = before[key]
        for key in self._blank_keys:
            if section.get(key) == "" and key in before:
                section[key] = before[key]
        # A section that only exists because we created it -- once our keys are
        # undone it is back to exactly what we seeded it with -- goes back to what
        # was there: nothing, or the non-object value (a hand-edited ``null``)
        # that was stored under the key. So a failed first-time save leaves
        # neither an empty scaffold, nor a copy of the legacy section shadowing
        # the original, nor a deleted value. A key someone else added to the
        # section since makes it theirs, and it stays.
        if not isinstance(self._prior, dict) and section == self._created_from:
            if self._prior is _ABSENT:
                fresh.pop(self._section, None)
            else:
                fresh[self._section] = self._prior
        return fresh

    async def commit(self) -> None:
        """Merge ``changes`` into the file. Raises ``ConfigReadError`` on a corrupt file."""
        # Looked up on the module at call time, not bound at import: the loader is
        # what tests patch to observe or interleave with this write.
        await drained_to_thread(
            functools.partial(_loader.update_config_locked, self._path, mutate=self.apply)
        )

    async def rollback(self, channel: str) -> None:
        """Undo ``commit`` after the paired ``.env`` write failed. Never raises."""
        try:
            await drained_to_thread(
                functools.partial(_loader.update_config_locked, self._path, mutate=self.restore)
            )
        except Exception:
            # A rollback that cannot run must not mask the original failure the
            # caller is already raising; the mismatch is logged instead.
            logger.exception("%s config rollback failed; config may lead .env", channel)


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


async def api_notification_agent_push(request: web.Request) -> web.Response:
    """POST /api/notifications/agent — send_notification MCP tool (RFC Phase 5).

    Agent sessions publish schema-v2 notifications through the system.agent
    channel. Body: ``{"title": str, "body"?: str, "priority"?: str,
    "url"?: str, "group_key"?: str, "actions"?: [{id,label,url?}]}``.
    ``source``/``channel`` are server-fixed (never body-supplied), and the
    full payload validation applies — internal-path urls, action caps,
    length caps. Durability mirrors the app push: a 200 awaits the persist.
    """
    state: DashboardState = request.app["state"]
    # App tokens must never reach this endpoint: an app's
    # declared ``permissions.api`` uses prefix-boundary matching, so an app
    # allowed ``/api/notifications`` is also admitted to this child route by
    # the auth middleware. This publish path is MCP/internal-secret only —
    # it publishes ``source="system"`` (channel system.agent), so an app
    # reaching it could impersonate system notifications and bypass its app
    # rate limits / declared-channel checks. Apps publish through
    # POST /api/notifications where their
    # token-verified ``app:<name>`` source is enforced. The middleware publishes
    # ``request["app"]`` on app-token auth and also on the internal-secret path
    # whenever the calling session resolves to an app, so this check bites for
    # an app agent arriving over MCP too.
    if request.get("app"):
        # Permission denial on a security boundary — audited before the
        # response (backend-security-controls: every denial emits SEL).
        _sel().log_api_access(
            caller=f"app:{request.get('app')}",
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="app tokens forbidden on the agent publish path",
        )
        return web.json_response({"error": "forbidden for app tokens"}, status=403)
    # MCP/internal-secret ONLY: the strict-internal
    # middleware also admits loopback dashboard-COOKIE callers to this
    # route, and a browser-credentialed caller publishing source="system"
    # would bypass MCP governance. The middleware sets
    # request["internal_auth"] only on the
    # validated X-Internal-Secret path — exactly the transport the
    # send_notification tool uses.
    if not request.get("internal_auth"):
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "internal-secret authentication required"}, status=403)
    # A caller whose own slot is GONE cannot be attributed. The
    # app-token check above refuses an app by name, but a tab closed while this
    # call was in flight takes the ``_app`` that check reads with it, so an
    # app-owned session going through that race would publish source="system"
    # here as though it were the person. Absence of an app claim is only
    # trustworthy for a caller that never had a slot (a Slack thread, a cron the
    # person owns); a ``dashboard:`` key names one, so a missing slot is a
    # failure to attribute rather than proof of the dashboard user. Refused HERE
    # rather than in the middleware: a popped slot no longer says whose tab it
    # was, so refusing centrally would also refuse the person's own in-flight
    # calls on every internal route. This route refuses because of what it
    # publishes -- ``source="system"`` on the system.agent channel.
    if caller_names_a_missing_slot(
        getattr(state, "_slots", None), request.headers.get("X-Session-Key", "")
    ):
        _sel().log_api_access(
            caller=str(request.headers.get("X-Session-Key") or ""),
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="calling session's slot is gone; cannot attribute a system publish",
        )
        return web.json_response(
            {"error": "calling session not found", "code": "caller_session_missing"},
            status=403,
        )
    # Bound the body BEFORE decoding, mirroring the app push endpoint: without
    # this the strict-internal route inherits the server-wide client_max_size,
    # and a large JSON object would be buffered and decoded on the event-loop
    # thread. Shared helper so the cap and the 413/400
    # contract cannot drift from the app push endpoint.
    body, _cap_err = await read_bounded_json(request)
    if _cap_err is not None:
        return _cap_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Type-check optional fields BEFORE payload construction: the bus
    # validator assumes str/list shapes, so a non-string url or non-list
    # actions would raise AttributeError/TypeError past the
    # NotificationValidationError catch -- a 500 where the contract says 400.
    for field_name in ("title", "body", "priority", "url", "group_key"):
        value = body.get(field_name)
        if value is not None and not isinstance(value, str):
            return web.json_response({"error": f"{field_name} must be a string"}, status=400)
    actions = body.get("actions")
    if actions is not None and not isinstance(actions, list):
        return web.json_response({"error": "actions must be a list"}, status=400)
    payload = NotificationPayload(
        source="system",
        channel="system.agent",
        kind="agent",
        title=body.get("title") or "",
        body=body.get("body") or "",
        priority=body.get("priority"),
        url=body.get("url"),
        group_key=body.get("group_key"),
        actions=actions,
    )
    try:
        note = state.notification_bus.push(payload)
    except NotificationValidationError as exc:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("agent notification delivery failed")
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="delivery failed",
        )
        return web.json_response({"error": "notification delivery failed"}, status=500)
    # Same durability guarantee as the app push endpoint: only acknowledge
    # once the persist job has succeeded.
    persist = state.last_notification_persist
    if persist is not None and not await persist:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="persistence failed",
        )
        return web.json_response({"error": "failed to persist notification"}, status=500)
    _sel().log_api_access(
        caller="agent",
        operation="notification_agent_push",
        outcome="success",
        source="notifications_api",
    )
    return web.json_response({"ok": True, "note": note})


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
                    inject_cls = json.dumps({"cronLabel": label})
                    # Queue while a turn is live -- same predicate the user-typed
                    # path uses (chat_handlers._api_chat).
                    if slot.running:
                        from kiro_crew.dashboard.slot_queue_repository import MAX_LIVE_QUEUE_ENTRIES

                        if len(slot._queue) >= MAX_LIVE_QUEUE_ENTRIES:
                            evicted = slot.queue_pop(0)
                            logger.warning(
                                "Queue full for slot %s — evicting oldest message", slot_key
                            )
                            _remove_queued_by_id(slot.messages, evicted["id"])
                        qid = slot.queue_append(wrapped, kind=CRON_NOTIFICATION_KIND)
                        _cls = json.loads(inject_cls)
                        _cls["queue_id"] = qid
                        slot.append("queued", wrapped, json.dumps(_cls))
                        state.push_slots_update()
                    else:
                        # circular import: chat_runner imports from
                        # kiro_crew.dashboard.handlers (for MAX_PROMPT_BYTES,
                        # _find_prompt, _list_aim_prompts), so we can't import
                        # it at module top-level without a cycle.
                        from kiro_crew.dashboard.chat_runner import _run_chat
                        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

                        # `cls` is not persisted for role `inject`, so the label
                        # must also ride in `meta`, which is — otherwise the row
                        # loses its identity on the next rehydrate.
                        slot.append(
                            "inject",
                            wrapped,
                            inject_cls,
                            meta={"injectKind": "cron", "cronLabel": label},
                        )
                        task = spawn_guarded_turn(
                            state,
                            slot,
                            _run_chat(
                                state,
                                slot,
                                wrapped,
                                _directive_user_origin=False,
                                # Structural provenance for the session ledger:
                                # the queued twin above carries
                                # CRON_NOTIFICATION_KIND, and this branch is the
                                # same injector dispatching directly.
                                _turn_actor="cron",
                            ),
                        )
                        slot.task = task
                        state.push_slots_update()
                    sent_session = True
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


async def api_slack_pins(request: web.Request) -> web.Response:
    """POST /api/slack/pins — pin/unpin/list pins on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. The gateway
    holds the token in ``state.slack_client``; this route enforces the same
    tracked-channel allowlist and SEL audit logging as the other Slack routes.

    Body: {"channel": "C...", "action": "add"|"remove"|"list", "ts": "..."}
    (``ts`` required for add/remove, ignored for list).
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("add", "remove", "list"):
        return web.json_response({"error": "action must be 'add', 'remove', or 'list'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)

    ts = body.get("ts", "")
    if action in ("add", "remove"):
        if not _is_slack_ts(ts):
            return web.json_response(
                {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
                status=400,
            )

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        result: dict[str, Any] = {"ok": True}
        if action == "add":
            await slack.add_pin(channel, ts)
        elif action == "remove":
            await slack.remove_pin(channel, ts)
        else:
            # Pinned messages may contain content originally posted by
            # LLM-controlled agents; redact each text field before returning
            # it to the caller (same output contract as send_message).
            pins = await slack.list_pins(channel)
            for pin in pins:
                safe_text, _ = redact_credentials(pin.get("text", ""))
                safe_text, _ = redact_exfiltration_urls(safe_text)
                pin["text"] = safe_text
            result["pins"] = pins
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(result)
    except Exception as e:
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


async def api_slack_reactions(request: web.Request) -> web.Response:
    """POST /api/slack/reactions — add/remove an emoji reaction on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. Mirrors the
    pins route: tracked-channel allowlist + SEL audit + server-held token.

    Body: {"channel": "C...", "ts": "...", "emoji": "white_check_mark",
           "action": "add"|"remove"}
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("add", "remove"):
        return web.json_response({"error": "action must be 'add' or 'remove'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    ts = body.get("ts", "")
    if not _is_slack_ts(ts):
        return web.json_response(
            {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
            status=400,
        )
    emoji = body.get("emoji", "")
    if not isinstance(emoji, str):
        return web.json_response({"error": "invalid emoji name"}, status=400)
    emoji = emoji.strip()
    if not emoji or not _EMOJI_NAME_RE.match(emoji):
        return web.json_response({"error": "invalid emoji name"}, status=400)

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        if action == "add":
            await slack.add_reaction(channel, ts, emoji, raise_on_error=True)
        else:
            await slack.remove_reaction(channel, ts, emoji, raise_on_error=True)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


def _missing_scope_message(needed: str) -> str:
    """Build an actionable missing_scope message, naming the scope(s) when known."""
    # Slack's ``needed`` field may name several comma-separated scopes.
    scopes = [s.strip() for s in needed.split(",") if s.strip()] if needed else []
    if scopes:
        joined = ", ".join(scopes)
        noun = "OAuth scope" if len(scopes) == 1 else "OAuth scopes"
        scope_clause = f"the {joined} {noun}"
        add_clause = f"add {joined} to"
    else:
        scope_clause = "an OAuth scope"
        add_clause = "add the required scope to"
    return (
        f"This Slack action requires {scope_clause}, which is not granted to this app. "
        "Reinstall the app after granting the required permissions in the Slack Dashboard. "
        f"Alternatively, {add_clause} the app manifest and recreate the app by following "
        "the steps in docs/guides/slack-setup.md."
    )


async def api_slack_profile(request: web.Request) -> web.Response:
    """POST /api/slack-profile — read a Slack user's profile."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    raw_user = body.get("user", "")
    if not isinstance(raw_user, str):
        return web.json_response({"error": "user must be a string"}, status=400)
    user_id = raw_user.strip()
    if not user_id:
        return web.json_response({"error": "user required"}, status=400)
    # Validate format first, then redact (#2)
    if not USER_ID_RE.match(user_id):
        return web.json_response({"error": "invalid user ID format"}, status=400)
    user_id, _ = redact_exfiltration_urls(user_id)
    user_id, _ = redact_credentials(user_id)

    # Authorization first (deny-by-default) — reject before any side effects
    from kiro_crew.slack.handler import is_allowed_user  # noqa: F811

    if not is_allowed_user(user_id):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "user not in allowlist"}, status=403)

    if not state.slack_client:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id} reason=slack_not_connected",
        )
        return web.json_response({"error": "Slack not connected"}, status=503)

    # Rate limiting: max 5 profile lookups per minute (#5)
    # Only counts authorized requests — unauthorized 403s don't consume slots
    now = time.monotonic()
    history: list[float] = getattr(state, "_profile_lookup_times", [])
    history = [t for t in history if now - t < 60]
    if len(history) >= 5:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id} reason=rate_limit",
        )
        return web.json_response(
            {"error": "rate limit exceeded — max 5 profile lookups per minute"}, status=429
        )
    history.append(now)
    state._profile_lookup_times = history  # type: ignore[attr-defined]

    try:
        profile = await state.slack_client.get_user_profile(user_id)
    except Exception as exc:
        from slack_sdk.errors import SlackApiError  # noqa: F811

        if isinstance(exc, SlackApiError):
            response = exc.response  # type: ignore[attr-defined]
            slack_error = str(response.get("error", "") or "") if response else ""
            if slack_error == "missing_scope":
                needed = str(response.get("needed", "") or "") if response else ""
                logger.warning(
                    "slack-profile: missing_scope (needed=%s) for %s", needed or "?", user_id
                )
                _sel().log_tool_invocation(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="error",
                    downstream_service="slack",
                    resources=f"user={user_id} reason=missing_scope needed={needed}",
                )
                needed, _ = redact_credentials(needed)
                needed, _ = redact_exfiltration_urls(needed)
                return web.json_response({"error": _missing_scope_message(needed)}, status=403)
        logger.exception("slack-profile: failed for %s", user_id)
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "Slack API error"}, status=502)

    # Redact free-form profile fields that could contain prompt-injection
    for key in list(profile):
        val = profile[key]
        if isinstance(val, str) and key not in ("id",):
            val, _ = redact_exfiltration_urls(val)
            val, _ = redact_credentials(val)
            profile[key] = val

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="read_slack_profile",
        outcome="completed",
        downstream_service="slack",
        resources=f"user={user_id}",
    )
    return web.json_response({"profile": profile})


def _deny_non_owner_browser_request(request: web.Request, operation: str) -> web.Response | None:
    """Require the dashboard owner on browser MUTATION endpoints. 403 or None.

    The caller must be the configured owner (``is_owner_dashboard_request``):
    app tokens, non-owner dashboard users, and callers with no identity are all
    refused. This is the same predicate used for MCP-app calls, source-provider
    mutations, and agent-question endpoints — one definition of "owner" across
    the dashboard.

    The mutations guarded here are security-sensitive:

    * installing the CLI globally adds an executable host capability, so a
      non-owner must not be able to mutate the machine to provide it;
    * the attach token is a stored credential that silences the browser's own
      per-attach approval prompt — the last human checkpoint before a program
      drives the user's logged-in session;
    * a browser download writes to the machine;
    * the view endpoints return an unauthenticated dashboard URL, i.e. control
      of a logged-in browser.

    Reads (``api_browser_install_get``) stay open: knowing whether browsing
    exists is not a capability. Mirrors the ComputerUse keystone precedent.
    """
    # Deferred import: source_providers imports chat state helpers from this
    # module's sibling, so a top-level import would close a cycle.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
    )

    if is_owner_dashboard_request(request):
        # A permission DECISION is audited whichever way it goes. Recording only
        # refusals leaves the log unable to answer "who armed browsing on this
        # host", which is the question an investigation actually asks: the
        # damaging path here is an ALLOWED install or token write, not a blocked
        # one.
        _sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation=operation,
            outcome="allowed",
            source="browser_api",
            resources=request.path,
        )
        return None
    # Derive a meaningful caller identity for the SEL record: app tokens
    # audit as "app:<name>"; dashboard users audit as their subject; callers
    # with no identity audit as "anonymous".
    app_name = request.get("app", "")
    if app_name:
        caller = f"app:{app_name}"
    else:
        caller = str(request.get("user") or "anonymous")
    # Permission denial on a security boundary — audited before the response
    # (backend-security-controls: every denial emits SEL).
    _sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="denied",
        source="browser_api",
        resources=request.path,
        error="browser mutations require the dashboard owner",
    )
    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    return _owner_denial_response(request, "dashboard user required", "dashboard_user_required")


async def api_browser_token_put(request: web.Request) -> web.Response:
    """PUT /api/browser/token -- set or clear the optional attach token.

    The response reports only WHETHER a token is set. A value that exists to reach a
    child process's environment has no reason to travel back out, and echoing it
    would put it in dashboard traffic and browser memory for no gain.

    Re-publishes the environment on the way out: a child reads the environment it
    was handed, so a token written without this would not reach any shell until the
    next gateway start.
    """
    denied = _deny_non_owner_browser_request(request, "browser_token_set")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Valid-but-non-object JSON ([], null, "str") would AttributeError on
    # body.get below -- an unintended 500 instead of a validation 400. Same
    # guard, same reason, as the other body-reading handlers in this file.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    value = body.get("token")
    if not isinstance(value, str):
        return web.json_response({"error": "token must be a string"}, status=400)
    await asyncio.to_thread(browser_cli_token.set_token, value)
    # Re-project onto the gateway's own environment, and DELETE the key when the
    # token was cleared: the agent inherits this env, so writing the file alone
    # would leave a revoked token live for every later invocation.
    #
    # Residual, stated rather than implied: a child process that ALREADY started
    # holds the value it inherited at spawn time, so an agent session running
    # right now keeps using the old token until it restarts. Revoking that would
    # mean killing live sessions on a settings write, which is a worse trade than
    # the window it closes.
    if browser_cli_token.has_token():
        os.environ.update(browser_cli_token.cli_env_overrides())
    else:
        os.environ.pop(browser_cli_token.TOKEN_ENV, None)
    return web.json_response({"ok": True, "token": browser_cli_token.has_token()})


async def api_browser_command(request: web.Request) -> web.Response:
    """POST /api/browser/command -- run one op against the native browser panel.

    Called by the ``browser`` MCP tool. Body:
    ``{"op": str, "session_key": str, "args"?: object, "timeout_ms"?: int}``.
    ``session_key`` is the BARE slot key the Electron panel registers under (the
    tool resolves and namespace-strips it; the ``dashboard:``-namespaced form
    travels in the ``X-Session-Key`` header for the peer check). Enqueues the op
    on the command bus and awaits the native panel's result.

    Responses:
    - 200 ``{"id", "ok": true, "result": <any>}`` -- op ran and succeeded;
    - 200 ``{"id", "ok": false, "error": str}`` -- op ran but failed;
    - 503 ``{"code": "no_native_panel"}`` -- no Electron poller for this session,
      returned FAST so the tool falls back to playwright-cli;
    - 429 ``{"code": "queue_full"}`` / 504 ``{"code": "timeout"}``.

    Proven ``X-Internal-Secret`` only (``request["internal_auth"] is True``),
    mirroring ``api_computer_use_invoke``: the middleware sets that flag only on
    the validated-secret path, so a dashboard COOKIE caller -- even a
    browser-credentialed page on a ``local_only=False`` deployment where strict
    paths reclassify as mixed -- is rejected here. We do NOT additionally require
    a loopback ``request.remote``: the tool reaches the gateway over the AF_UNIX
    internal-API socket (0700 data home + kernel peer check), where
    ``request.remote`` is empty, so a loopback re-assert would 403 every op.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    op = body.get("op")
    args = body.get("args")
    timeout_ms = body.get("timeout_ms")
    session_key = body.get("session_key")
    if not isinstance(op, str) or not op:
        return web.json_response({"error": "op required", "code": "op_required"}, status=400)
    if args is not None and not isinstance(args, dict):
        return web.json_response(
            {"error": "args must be an object", "code": "args_must_be_object"}, status=400
        )
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        timeout_ms = DEFAULT_COMMAND_TIMEOUT_MS
    if not isinstance(session_key, str) or not session_key:
        # No addressable panel -> answer like the no-panel case (503) so the tool
        # falls back to playwright-cli rather than surfacing a hard error.
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    bus = get_command_bus()
    logger.debug("browser-cmdbus: submit op=%s session=%s", op, session_key)
    try:
        outcome = await bus.submit(session_key, op, args or {}, timeout_ms=timeout_ms)
    except NoPanelError:
        logger.debug(
            "browser-cmdbus: no native panel registered for session=%s -> 503 (client falls back to playwright-cli)",
            session_key,
        )
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    except QueueFullError:
        return web.json_response({"error": "queue-full", "code": "queue_full"}, status=429)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout", "code": "timeout"}, status=504)
    response: dict[str, Any] = {"id": outcome.get("id"), "ok": bool(outcome.get("ok"))}
    if outcome.get("ok"):
        response["result"] = outcome.get("result")
    else:
        response["error"] = outcome.get("error") or "error"
    logger.debug(
        "browser-cmdbus: op=%s completed ok=%s session=%s", op, bool(outcome.get("ok")), session_key
    )
    return web.json_response(response)


async def api_browser_command_drain(request: web.Request) -> web.Response:
    """POST /api/browser/command-drain -- long-poll for a queued browser command.

    Called by the Electron main process. Body:
    ``{"session_keys": [str, ...], "wait_ms"?: int}``.

    SIDE EFFECT: registers ``session_keys`` as having a live native panel for a
    fixed liveness window (independent of ``wait_ms``) AND marks a native host
    present for the same window; the registration is what ``/api/browser/command``
    checks to decide whether to 503, and host-presence is what lets it briefly
    wait for a cold-starting panel instead. ``wait_ms == 0`` with empty
    ``session_keys`` is the Electron idle heartbeat: it refreshes host-presence
    and returns 204 at once.

    Responses: 200 ``{"id", "session_key", "op", "args"}`` or 204 (nothing yet).
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_drain",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    session_keys = body.get("session_keys")
    if not isinstance(session_keys, list) or not all(isinstance(k, str) for k in session_keys):
        return web.json_response(
            {"error": "session_keys must be a list of strings", "code": "session_keys_invalid"},
            status=400,
        )
    wait_ms = body.get("wait_ms")
    # ``wait_ms == 0`` is a valid heartbeat: refresh host-presence and return 204
    # at once. Only a missing, negative, or non-int value takes the long wait.
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or wait_ms < 0:
        wait_ms = DEFAULT_DRAIN_WAIT_MS
    bus = get_command_bus()
    command = await bus.drain(session_keys, wait_ms=wait_ms)
    if command is None:
        return web.Response(status=204)
    return web.json_response(command)


async def api_browser_command_result(request: web.Request) -> web.Response:
    """POST /api/browser/command-result -- post a native browser command's result.

    Called by the Electron main process. Body:
    ``{"id": str, "ok": bool, "result"?: <any>, "error"?: str}``.

    Responses: 200 ``{"ok": true}``, or 404 ``{"code": "unknown_command"}`` when
    the id already timed out or never existed.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_result",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    command_id = body.get("id")
    if not isinstance(command_id, str) or not command_id:
        return web.json_response({"error": "id required", "code": "id_required"}, status=400)
    ok = bool(body.get("ok"))
    result = body.get("result")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    bus = get_command_bus()
    matched = await bus.complete(command_id, ok, result=result, error=error)
    if not matched:
        return web.json_response(
            {"error": "unknown-command", "code": "unknown_command"}, status=404
        )
    return web.json_response({"ok": True})


def _browser_install_job(state: DashboardState) -> browser_install_job.BrowserInstallJob | None:
    """The gateway's current or most recent install job, if one ran."""
    job = getattr(state, "_browser_install_job", None)
    return job if isinstance(job, browser_install_job.BrowserInstallJob) else None


def _browser_install_active(state: DashboardState) -> bool:
    """Whether an install occupies the gateway's one slot.

    The task is consulted as well as the job so the slot stays closed until
    the task that owns the worker has actually returned.
    """
    job = _browser_install_job(state)
    task = getattr(state, "_browser_install_task", None)
    return bool((job is not None and job.running) or (task is not None and not task.done()))


def _browser_install_status(state: DashboardState) -> dict[str, Any]:
    """The job-derived fields every install response carries.

    ``installing`` and ``last_error`` keep their meaning for older dashboards:
    whether a job runs, and the latest job's detail when it failed.
    """
    job = _browser_install_job(state)
    failed = job is not None and job.status == browser_install_job.STATUS_FAILED
    return {
        "installing": _browser_install_active(state),
        "install_job": job.snapshot() if job is not None else None,
        "last_error": job.error_detail if failed and job is not None else None,
    }


def _browser_install_conflict(state: DashboardState) -> web.Response:
    """409 naming the job that holds the slot, so the panel can say which one."""
    job = _browser_install_job(state)
    return web.json_response(
        {
            "error": "an install is already running",
            "code": "install_already_running",
            "install_job": job.snapshot() if job is not None and job.running else None,
        },
        status=409,
    )


def _start_browser_install_job(
    state: DashboardState,
    kind: str,
    engine: str | None,
    work: Callable[[browser_cli_install.StageCallback], dict[str, Any]],
    default_step: str,
) -> None:
    """Publish a new running job, then start its worker.

    The job is on ``state`` before the task exists, so a status read that
    lands between this call and the worker's first stage already reports it.
    Stage updates arrive from the worker thread and are marshalled onto the
    loop; each is bound to THIS job object, whose own id and status checks make
    a late update from a finished job a no-op.

    Cancelling the task (gateway shutdown cancels every pending task) kills
    the installer's process tree through the job's
    :class:`~kiro_crew.browser_cli.install.InstallScope` before the task
    ends, because cancelling the awaiting coroutine alone leaves the worker
    thread and its subprocess running.
    """
    loop = asyncio.get_running_loop()
    job = browser_install_job.BrowserInstallJob.start(kind, engine)
    scope = browser_cli_install.InstallScope()
    state._browser_install_job = job
    state._browser_install_scope = scope

    def _on_stage(stage: str) -> None:
        # Runs on the worker thread; the loop owns the job.
        loop.call_soon_threadsafe(job.apply_stage, job.id, stage)

    async def _run() -> None:
        try:
            result = await asyncio.to_thread(
                browser_cli_install.run_in_scope, scope, work, _on_stage
            )
        except asyncio.CancelledError:
            await _terminate_install_scope(scope)
            job.finish(
                job.id,
                browser_install_job.STATUS_INTERRUPTED,
                browser_install_job.ERROR_INTERRUPTED,
                "interrupted: the gateway stopped this install",
            )
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            # Redact the full text, then truncate: see bounded_detail. Pinned by
            # test_a_credential_straddling_a_pre_redaction_cut_is_still_masked.
            job.finish(
                job.id,
                browser_install_job.STATUS_FAILED,
                browser_install_job.ERROR_EXCEPTION,
                browser_install_job.bounded_detail(str(exc)),
            )
            return
        status, code, detail = browser_install_job.outcome_of(result, default_step)
        if scope.terminated and status != browser_install_job.STATUS_INTERRUPTED:
            status = browser_install_job.STATUS_INTERRUPTED
            code = browser_install_job.ERROR_INTERRUPTED
        job.finish(job.id, status, code, detail)

    state._browser_install_task = asyncio.create_task(_run())


async def _terminate_install_scope(scope: Any) -> None:
    """Kill an install scope's children off the event loop.

    ``InstallScope.terminate`` runs ``taskkill`` on Windows, a blocking call with
    its own timeout, so it goes to a worker thread. At interpreter teardown the
    default executor may already refuse work; the call then runs inline, since
    nothing else is left on the loop to stall.
    """
    try:
        await asyncio.to_thread(scope.terminate)
    except RuntimeError:
        scope.terminate()


async def stop_browser_install(state: DashboardState) -> None:
    """Terminate the running install's subprocesses and wait for its task.

    For the gateway's shutdown path. Idempotent and never raises. The job is
    left ``interrupted``; nothing is persisted or resumed, so the next gateway
    starts with no job and the operator retries explicitly.
    """
    scope = getattr(state, "_browser_install_scope", None)
    if isinstance(scope, browser_cli_install.InstallScope):
        await _terminate_install_scope(scope)
    task = getattr(state, "_browser_install_task", None)
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        # ``wait`` reports the child's outcome instead of raising it here, so the
        # CancelledError the child raises because it was just cancelled never
        # reaches this frame, while a cancellation of THIS task (the gateway's
        # shutdown deadline expiring around ``_shutdown()``) still propagates out
        # of the await. A plain ``await task`` inside ``except CancelledError``
        # could not tell the two apart and swallowed both.
        await asyncio.wait({task})
        if not task.cancelled():
            # Consume a failure so the loop does not log "exception was never
            # retrieved" at teardown; the job record already carries it.
            task.exception()


async def api_browser_install_get(request: web.Request) -> web.Response:
    """GET /api/browser/install -- whether browsing is available, and why not.

    Reports the install job separately from the detection fields so the card can
    show progress for an install already in flight, including one started by a
    different dashboard tab: the job lives on the gateway, not in a page. A read
    never spawns an installer, launches a browser, or attaches to one.
    """
    state: DashboardState = request.app["state"]
    payload = dict(await asyncio.to_thread(browser_cli_install.detect))
    payload.update(_browser_install_status(state))
    payload["token"] = browser_cli_token.has_token()
    return web.json_response(payload)


async def api_browser_install_start(request: web.Request) -> web.Response:
    """POST /api/browser/install -- install the Playwright CLI in the background.

    Returns immediately with the same shape as the GET. The install downloads a
    browser, which takes long enough that holding the request open would read as a
    hung dashboard, so progress is observed by re-reading rather than awaited here.

    A click while CLI setup already runs is folded into that job: it is the same
    work, and npm is not safe to run twice over the same target at once. A click
    while an ENGINE download runs is refused with 409 naming that job, because
    folding it would answer "CLI setup accepted" for work that is not happening.
    """
    denied = _deny_non_owner_browser_request(request, "browser_cli_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    if _browser_install_active(state):
        job = _browser_install_job(state)
        if job is not None and job.running and job.kind != browser_install_job.KIND_CLI_SETUP:
            return _browser_install_conflict(state)
    else:
        _start_browser_install_job(
            state,
            browser_install_job.KIND_CLI_SETUP,
            None,
            lambda on_stage: browser_cli_install.install(on_stage=on_stage),
            "install",
        )
    return await api_browser_install_get(request)


async def api_browser_engine_install(request: web.Request) -> web.Response:
    """POST /api/browser/engine -- download one engine's browser build.

    Body: ``{"engine": "chromium" | "firefox" | "webkit"}``.

    Shares the ONE install slot with the CLI install rather than taking its own:
    both drive the same browser installer, which is not safe to run twice over the
    same cache at once.
    """
    denied = _deny_non_owner_browser_request(request, "browser_engine_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # `(body or {})` absorbs [] and null but NOT a non-empty non-dict ([1],
    # "abc", 5), which would AttributeError into a 500. Check the type instead
    # of leaning on falsiness.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    engine = str(body.get("engine", "")).strip()
    # Validated here as well as in install_browser: rejecting at the boundary
    # keeps an unknown value out of the background task entirely, so the operator
    # gets a 400 instead of an error they have to go re-read the status to find.
    if engine not in browser_cli_install.BROWSER_ENGINES:
        return web.json_response({"error": "unknown engine", "code": "unknown_engine"}, status=400)
    if _browser_install_active(state):
        # 409, NOT a folded success. Engines are three DISTINCT targets sharing
        # one slot, so answering 200 while a different engine installs makes the
        # panel show WebKit downloading when Firefox actually is.
        return _browser_install_conflict(state)
    _start_browser_install_job(
        state,
        browser_install_job.KIND_ENGINE_DOWNLOAD,
        engine,
        lambda on_stage: browser_cli_install.install_browser(engine, on_stage=on_stage),
        "install-browser",
    )
    return await api_browser_install_get(request)


def _browser_view_payload() -> dict[str, Any]:
    """``browser_cli_view.status()`` plus where the panel should FRAME it.

    ``path`` is the dashboard-origin relay (``/browser-view/<token>/``): same
    origin as the dashboard, so it is reachable wherever the dashboard is — an
    SSH forward, a tunnel — with no second port. The embedded per-instance
    capability token IS the relay's authentication (the panel frames it in an
    opaque-origin sandbox that sends no cookies), and THIS payload — served
    only through the cookie-authed, owner-gated view endpoints — is its sole
    disclosure point, so possession proves the holder passed the owner gate.
    Null unless the view is running, so the field can never frame a dead
    relay. The absolute ``url`` stays in the payload for direct loopback use
    and older frontends — but it is only ever published alongside a target
    the ownership proof vouched for: when ``relay_target()`` refuses (the
    child exited between the two lock holds, or the proof was inconclusive),
    the whole payload degrades to ``stopped`` rather than offering the stale
    direct ``url`` as a frameable fallback for whatever wins the freed port.
    The panel polls, so a live view is re-reported on the next cycle.
    """
    payload = browser_cli_view.status()
    if payload.get("status") != "running":
        payload["path"] = None
        return payload
    target = browser_cli_view.relay_target()
    if target is None:
        payload["status"] = "stopped"
        payload["url"] = None
        payload["port"] = None
        payload["path"] = None
        return payload
    payload["path"] = f"{ROUTE_PREFIX}/{target[1]}/"
    return payload


async def api_browser_view_get(request: web.Request) -> web.Response:
    """GET /api/browser/view -- where the Playwright CLI dashboard is served.

    Reports without starting anything, so polling the panel never launches a
    browser dashboard the operator did not ask for.

    App-token denied like the install and token routes: the reply carries the
    dashboard URL, and that URL is served WITHOUT authentication, so handing it
    to an app is handing over control of a logged-in browser. Read-only on this
    gateway is not read-only on the browser. (The ``path`` field embeds the
    relay's capability token — this owner-gated endpoint is that token's only
    disclosure point, so it stays denied to apps for the same reason as the
    raw URL.)
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_status")
    if denied is not None:
        return denied
    return web.json_response(await asyncio.to_thread(_browser_view_payload))


async def api_browser_view_start(request: web.Request) -> web.Response:
    """POST /api/browser/view/start -- ensure the CLI dashboard is serving.

    Idempotent, and off-loaded to a thread because starting the dashboard waits on
    a child process becoming healthy, which would otherwise stall the event loop
    and with it every other dashboard request. Honors
    ``dashboard.browser_view_port`` when set, so a remote-gateway operator can
    keep the port inside their tunnel's forwarded set.

    App-token denied: this both LAUNCHES a browser process and returns the
    unauthenticated dashboard URL, so it is the stronger half of the same hole
    the GET carries.
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_start")
    if denied is not None:
        return denied

    def _start_view() -> None:
        # Config read stays in the worker thread with the child-process wait:
        # both are blocking I/O that must not run on the event loop.
        pinned = KiroCrewConfig.load().dashboard.browser_view_port
        browser_cli_view.ensure_running(pinned or None)

    await asyncio.to_thread(_start_view)
    return web.json_response(await asyncio.to_thread(_browser_view_payload))


async def api_browser_open(request: web.Request) -> web.Response:
    """POST /api/browser/open -- show a URL the owner typed in the gateway's browser.

    Body: ``{"url": "https://...", "session_key": "<chat slot>"}``. The Browser
    panel calls this on the non-native transport (a plain browser tab, including
    one reaching a remote gateway over a tunnel) when the typed host is not
    loopback: nothing else can render an external site there, because the
    dashboard CSP refuses to frame it. The handler makes sure the ``show`` view is
    serving, runs ``playwright-cli -s=<session> goto|open <url>`` as a supervised
    child (see :mod:`kiro_crew.browser_cli.launcher`), and answers ``{ok,
    session, error, view}`` -- ``error`` is the CLI's own text, verbatim, so the
    panel can show WHY (``No usable sandbox!``, a missing Chromium build) instead
    of a blank frame; ``view`` is the post-attempt ``show`` status so the panel
    can frame it without a second round trip.

    Owner-only, like the view routes, and it additionally REFUSES a caller
    authenticated by the internal secret: agent browsing goes through the shell
    approval ladder (capability model), and an agent reaching this endpoint would
    bypass it. A human pressing Enter in an authenticated dashboard is the consent
    this route rests on. Off-loaded to a thread because ``open`` waits on a
    Chromium launch, which must not stall the event loop.
    """
    denied = _deny_non_owner_browser_request(request, "browser_open")
    if denied is not None:
        return denied
    if request.get("internal_auth"):
        # Belt and braces: the route is not on any internal-path list, so the
        # secret is ignored before this handler runs; this pins the refusal even
        # if the route is ever added to one.
        _sel().log_api_access(
            caller="internal",
            operation="browser_open",
            outcome="denied",
            source="browser_api",
            resources=request.path,
            error="internal-secret callers may not drive the browser panel",
        )
        return web.json_response(
            {"error": "forbidden for internal callers", "code": "internal_caller"}, status=403
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    raw_url = body.get("url")
    session_key = body.get("session_key")
    if not isinstance(raw_url, str) or not isinstance(session_key, str) or not session_key.strip():
        return web.json_response(
            {"error": "url and session_key are required", "code": "invalid_request"}, status=400
        )
    url = browser_cli_launcher.validate_url(raw_url)
    if url is None:
        return web.json_response(
            {
                "error": (
                    "url must be http(s) with a host and no credentials, "
                    "query, or fragment (a secret in the URL would leak via argv)"
                ),
                "code": "invalid_url",
            },
            status=400,
        )

    def _launch() -> dict[str, Any]:
        # Same start path as api_browser_view_start, in the worker thread with the
        # child-process wait: the view must be up for the human to see the page,
        # and its singleton socket is what the launcher's reveal talks to.
        pinned = KiroCrewConfig.load().dashboard.browser_view_port
        browser_cli_view.ensure_running(pinned or None)
        result = browser_cli_launcher.open_url(url, session_key.strip())
        payload = result.as_dict()
        payload["view"] = _browser_view_payload()
        return payload

    return web.json_response(await asyncio.to_thread(_launch))


def _mask_secret(val: str) -> str:
    """Return a masked preview keeping the token prefix + last 4 chars.

    e.g. "xoxb-1234-abcd…wxyz" → "xoxb-••••wxyz". Empty string for no value.
    """
    if not val:
        return ""
    prefix = f"{val.split('-', 1)[0]}-" if "-" in val else ""
    tail = val[-4:] if len(val) >= 4 else ""
    return f"{prefix}••••{tail}"


def _clean_id_list(raw: object, is_valid: Callable[[str], bool], label: str) -> list[str]:
    """Validate and normalize a list of ID strings, dropping blanks.

    Raises ``ValueError`` (message safe to surface) when *raw* is not a list or
    an entry fails *is_valid*. Shared by the channel / enterprise-org fields.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{label}s must be a list")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if not is_valid(s):
            raise ValueError(f"invalid {label}: {s}")
        out.append(s)
    return out


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
    parse). That refusal is raised here as a 409 carrying the fix, so every
    channel save answers it the same way instead of with an opaque 500. Every
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


async def _write_env_or_roll_back(
    env_updates: dict[str, str | None],
    rollback: Callable[[], Awaitable[None]] | None,
) -> None:
    """Write a channel save's ``.env`` half, undoing its config half on failure.

    Every channel saver commits ``config.json`` first and then calls this, so the
    second half of the transaction exists once and a new saver cannot copy only
    part of it. *rollback* undoes the config commit; pass ``None`` when the save
    wrote no config.

    A failed write runs *rollback* and re-raises, so a refused save (the wide
    ``.env`` 409 included) changes nothing. A cancellation drains the write
    first and runs *rollback* only when the write itself failed: a write that
    landed before the cancel arrived already pairs with the committed config,
    and rolling the config back would split the pair it means to keep. The
    cancellation is re-raised either way.

    On success the process environment is updated to match: ``load_credentials()``
    lets ``os.environ`` win over ``.env``, so a replaced or cleared token would
    otherwise keep reading as installed until restart, and spawned children
    would inherit the stale value.
    """
    write = asyncio.ensure_future(_write_env_off_loop(env_updates))
    try:
        await asyncio.shield(write)
    except asyncio.CancelledError:
        await asyncio.gather(write, return_exceptions=True)
        failed = not write.cancelled() and write.exception() is not None
        if failed and rollback is not None:
            await rollback()
        raise
    except BaseException:
        if rollback is not None:
            await rollback()
        raise
    for key, new_val in env_updates.items():
        if new_val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = new_val


def _wide_env_refusal(exc: _loader.EnvFileWideEncodingError) -> web.HTTPConflict:
    """The response for a channel save refused because ``.env`` is wide-encoded."""
    message = (
        f"{_loader.env_path()} is saved as {exc.wide_encoding}, which Kiro Crew "
        "cannot read. Re-save it as UTF-8 and save again. The .env was not changed."
    )
    return web.HTTPConflict(
        text=json.dumps({"error": message}),
        content_type="application/json",
    )


def _write_env_updates(updates: dict[str, str | None]) -> None:
    """Update select keys in config_dir/.env, preserving comments and order.

    A value of ``None`` deletes the key; new keys are appended. The write goes
    through :func:`kiro_crew.atomic_write.atomic_write` with
    ``restrict_to_owner=True``, which locks the unique temp file down to its
    owner BEFORE any content byte is written: POSIX mode bits protect nothing
    on Windows (``fchmod_safe`` is a documented no-op there), so a lockdown
    applied after the write would leave the tokens readable under the
    directory-inherited DACL for the whole write — and indefinitely if the
    lockdown fails. ``restrict_on_error="warn"`` keeps this writer's contract:
    a host where the lockdown cannot be applied must not abort a token save
    that already succeeded.

    CALLER CONTRACT: blocking, and every caller is an async request handler
    holding ``_get_config_lock()``, so each one must reach this through
    ``_write_env_off_loop`` rather than ``asyncio.to_thread`` directly --
    the drain there is what keeps the lock from being released mid-write.
    Offload the WHOLE call, never a part of it: the read-modify-write is one
    transaction, and a suspension point between the read and the rename would
    let a concurrent writer's keys be dropped by a write derived from lines
    nobody re-read. ``test/test_channel_env_write_off_loop.py`` pins both
    properties for all six channels.
    """
    ep = _loader.env_path()
    # Ensure the parent exists before opening the lock file (the .env itself may
    # not exist yet — e.g. first credential save into a fresh config dir).
    ep.parent.mkdir(parents=True, exist_ok=True)
    # Serialize this read-modify-write against the OTHER cross-process .env
    # writers (the `kirocrew secrets import` migrator and the WeChat/Weixin QR
    # handler) on the SAME advisory lock, derived from the shared helper so all
    # writers provably use one lock file. Without this, a channel/token save
    # here could interleave with the importer's rewrite (read stale bytes here,
    # or clobber the importer's commit), losing a freshly saved token or leaving
    # a `secret://` reference pointing at the wrong value. The lock wraps the
    # entire read → transform → atomic-rename sequence.
    # Lazy import: `secrets.migrate` is the CLI migration module and must stay
    # OFF the gateway boot path (messaging.py is imported at startup). Importing
    # it inside the writer keeps it off the module-import path so gateway
    # readiness is not delayed by loading the migration module.
    from kiro_crew.secrets.migrate import _env_lock_path

    lock_path = _env_lock_path(ep)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
        os.close(lock_fd)
        raise OSError(
            f"{ep} is locked by another process (a secrets import or another "
            "credential save is in progress); no update performed. Retry once "
            "the other operation finishes."
        )
    try:
        _write_env_updates_locked(ep, updates)
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)


def _write_env_updates_locked(ep: "Path", updates: dict[str, str | None]) -> None:
    """The read-modify-atomic-rewrite of .env, run under the .env lock held by
    the caller (:func:`_write_env_updates`)."""

    lines = _loader.read_env_text(ep, encoding="utf-8").splitlines() if ep.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                seen.add(k)
                new_val = updates[k]
                if new_val is None:
                    continue
                out.append(f"{k}={new_val}")
                continue
        out.append(line)
    for k, new_val in updates.items():
        if k not in seen and new_val:
            out.append(f"{k}={new_val}")
    content = "\n".join(out) + ("\n" if out else "")
    atomic_write(
        ep,
        _loader.env_bom_prefix(ep) + content,
        restrict_to_owner=True,
        restrict_on_error="warn",
    )


async def api_slack_manifest(request: web.Request) -> web.Response:
    """GET /api/slack/manifest — rendered Slack app manifest + create URL.

    Mirrors ``kirocrew manifest --url`` so the settings UI can offer one-click
    Slack app creation without the CLI: the bundled template gets the user's
    alias substituted, and the comment-stripped YAML is URL-encoded into
    Slack's new-app deep link. Serves only the public template — no secrets.
    """
    from kiro_crew import slack_manifest

    # Default to a non-identifying alias: $USER is a host account name and
    # should not be volunteered to every authenticated client.
    alias = request.query.get("alias", "").strip() or "kirocrew"
    if not slack_manifest.valid_alias(alias):
        return web.json_response({"error": "invalid alias"}, status=400)
    try:
        rendered = slack_manifest.render(alias)
        create_url = slack_manifest.deep_link(alias)
    except FileNotFoundError:
        return web.json_response({"error": "manifest template missing"}, status=500)
    return web.json_response(
        {
            "alias": alias,
            "manifest": rendered,
            "create_url": create_url,
        }
    )


async def api_channel_folder_backfill(request: web.Request) -> web.Response:
    """POST /api/channel-folders/backfill - file a channel's EXISTING conversations.

    One endpoint for all channels rather than one per channel: the namespace
    arrives in the body and the work is byte-identical for every one of them, so
    ten copies would be ten places for the eligibility guard to drift apart.

    Loopback-only, matching the config saves it sits beside. It writes no
    credential and no config, so that is not inherited reasoning: it bulk-moves
    conversations with no collective undo, and a remote caller can neither see
    the sidebar it rearranges nor put anything back.

    Answers 200 with the report even when nothing moved, because "nothing to do"
    is a normal outcome the panel has to render (and ``reason`` says which kind
    it was). A 4xx is reserved for a request that was never actionable.
    """
    caller = request.get("user", "dashboard")

    def _deny(msg: str, code: str, status: int = 400) -> web.Response:
        # The ``code`` rides in the dict LITERAL beside the message, which is what
        # makes the body machine-readable at any status: the panel renders `error`,
        # while a caller that needs to branch reads `code` rather than matching on
        # prose that translation or rewording can change under it.
        _sel().log_api_access(
            caller=caller,
            operation="channel.folder.backfill",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg, "code": code}, status=status)

    if not is_direct_local_request(request):
        # Deliberately NOT the neighbouring panels' wording ("read-only from
        # remote sessions"). This endpoint's own button says "File existing
        # sessions", meaning chat conversations, so a refusal that says
        # "sessions" meaning LOGIN sessions puts one word for two different
        # things on one card -- a blind reader could not tell which was meant.
        #
        # It is also a whole sentence naming the remedy, not a fragment: a
        # reader who does not already know what "the local machine" is has
        # nothing to act on, which is a dead end rather than a refusal.
        #
        # And it NAMES that machine. "The computer that hosts this dashboard"
        # tells a remote reader what kind of computer to look for, not which
        # one: a blind read of that sentence recorded "I have no idea how I'd
        # find out which computer that is". The host's first DNS label is the
        # same stand-in `session_transfer.local_instance_label` uses for the
        # same reader, and the same fallback shape: a host with no name keeps
        # the description alone, never a blank or an exception on a refusal path.
        # The `code` is unchanged, so nothing machine-readable moves with this.
        try:
            host = platform.node().split(".")[0]
        except Exception:
            host = ""
        # It also states the reader's own situation first. A remote viewer IS
        # looking at a dashboard, so "open the dashboard there" read as circular
        # to a blind reader who then stopped; "this same page on <host>" tells
        # them what to do with the name.
        where = (
            f"{host}, the computer that hosts this dashboard"
            if host
            else "the computer that hosts this dashboard"
        )
        there = f"on {host}" if host else "there"
        return _deny(
            "You are viewing this page from another computer. "
            f"Filing runs only on {where}. "
            f"Open this same page {there} and click again.",
            "read_only_remote",
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", "invalid_json")
    if not isinstance(body, dict):
        return _deny("body must be an object", "invalid_body")
    raw_namespace = body.get("namespace")
    if not isinstance(raw_namespace, str):
        return _deny("namespace must be text", "namespace_invalid")
    namespace = raw_namespace.strip().lower()
    # Closed set, checked here rather than left to the config read: the namespace
    # selects a config section and stamps a folder, and an unrecognised one must
    # be a refusal the caller can see, not a silently empty pass.
    if namespace not in CHANNEL_CONFIG_SECTIONS:
        return _deny("unknown channel", "unknown_channel")
    state = request.app.get("state")
    if state is None:
        return _deny("dashboard state unavailable", "state_unavailable", status=503)

    report = await backfill_channel_folder(state, namespace)
    _sel().log_api_access(
        caller=caller,
        operation="channel.folder.backfill",
        outcome="ok",
        source="dashboard",
        # The count, not the keys: a session key names a channel conversation and
        # the audit log is not the place to enumerate which ones a user filed.
        resources=f"{namespace}:{len(report['moved'])}",
    )
    return web.json_response(report)


def _threshold_pct_rejection(body: dict[str, Any], key: str) -> tuple[str, str] | None:
    """``(code, message)`` when *body[key]* is present and not a valid percentage.

    ``None`` when the key is absent or the value is in range. One home for the check
    across every channel that exposes context-window nudge thresholds, because
    ``isinstance(True, int)`` is True in Python: each site has to exclude bool
    explicitly or a JSON ``true`` reads as 1%, and a per-channel copy of that subtlety
    is a per-channel chance to omit it. The bounds come from the config loader, so a
    value this accepts is one the loader will not silently clamp.
    """
    if key not in body:
        return None
    from kiro_crew.config.loader import THRESHOLD_PCT_MAX, THRESHOLD_PCT_MIN

    pct = body.get(key)
    if isinstance(pct, int) and not isinstance(pct, bool):
        if THRESHOLD_PCT_MIN <= pct <= THRESHOLD_PCT_MAX:
            return None
    return (
        f"{key}_invalid",
        f"{key} must be an integer between {THRESHOLD_PCT_MIN} and {THRESHOLD_PCT_MAX}",
    )


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
# it into this interpreter. Teams (PyJWT) and WhatsApp (neonize) have the same
# shape and are one entry each when their panels adopt the card.
_CHANNEL_SDK_EXTRA: dict[str, tuple[str, str]] = {
    "feishu": ("lark_oapi", "feishu"),
}


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.dashboard.handlers.messaging.<name>`` reaches it wherever it lives;
# see ``kiro_crew.dashboard.messaging_api``. Run once, after this body has bound
# every name and before the owner-surface guard below wraps the spawn routes.
_messaging_api.compose(
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
