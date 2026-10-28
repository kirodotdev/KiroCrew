"""Slack Socket Mode event routing.

Sets up the Socket Mode client, dispatches incoming events to the
correct handler:

- ``interactive`` → :mod:`interactions.dispatch`
- ``slash_commands`` → registry-based sub-command routing
- ``member_joined_channel`` → tracking-channel allowlist prompt
- ``app_home_opened`` → publish Home Tab view
- ``message`` / ``app_mention`` → :func:`handler.handle_message`

The bounded dedup cache that prevents processing the same Slack event
twice (``SeenCache``) lives in :mod:`kiro_crew.slack.events_inbound`.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib as _importlib
import inspect
import json
import logging
import os
import re
import sys as _sys
import tempfile
import weakref
from collections import OrderedDict
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Literal,
    overload,
)

import aiohttp
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import __version__, slack_manifest
from kiro_crew.agent_discovery import agent_spec_stems
from kiro_crew.agent_spec_format import (
    is_markdown_spec,
    iter_agent_spec_files,
    parse_agent_spec_text,
)
from kiro_crew.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    KiroCrewConfig,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.cron import format_schedule
from kiro_crew.dashboard.chat_utils import run_config_write
from kiro_crew.dashboard.handlers import get_update_info
from kiro_crew.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, parse_duration
from kiro_crew.executors import subprocess_executor
from kiro_crew.hooks import safe_read_file
from kiro_crew.mcp_discovery import list_servers
from kiro_crew.messaging.commands import note_user_stop
from kiro_crew.messaging.dispatch import admit_inbound_callback
from kiro_crew.messaging.identity import channel_inbound_permitted
from kiro_crew.messaging.link import canonical_key, legacy_key
from kiro_crew.messaging.queue_drain import (
    entries_queued_by,
    owner_token,
    register_drain,
    tag_entry,
)
from kiro_crew.platform import current_context, safe_context_call
from kiro_crew.platform.interfaces import InterceptDecision
from kiro_crew.safety_override import safety_override, yolo_policy_permits
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
    should_record_observe_history,
)
from kiro_crew.sel import sel
from kiro_crew.session import unlink_queued_temp_paths
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
)
from kiro_crew.session_lifecycle import compaction_in_flight as _compaction_in_flight
from kiro_crew.session_lifecycle import (
    consume_stop_declined,
    decline_stop,
    hand_queue_to_successor,
)
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.agent_sessions.session_events import AGENT_SESSION_EVENTS
from kiro_crew.slack.agent_sessions.session_events import dispatch as dispatch_agent_session_event
from kiro_crew.slack.allowlist import prompt_track_channel, send_dashboard_link
from kiro_crew.slack.blocks import (
    build_stopping_blocks,
    channels_modal,
    command_hint_block,
    dashboard_link_block,
    voice_config_modal,
)
from kiro_crew.slack.channel_policy import SlackChannelRefused
from kiro_crew.slack.enterprise import trusted_bot_admission, validated_self_user_id
from kiro_crew.slack.files import (
    VOICE_MEMO_DURATION_UNVERIFIED,
    VOICE_MEMO_FAILED,
    VOICE_MEMO_TOO_LONG,
    VOICE_MEMO_UNAVAILABLE,
    is_voice_memo,
    process_slack_files,
    voice_memo_notes,
)
from kiro_crew.slack.handler import (
    APPROVAL_AUTO,
    APPROVAL_INTERACTIVE,
    describe_grant_lifetime,
    handle_message,
    is_allowed_user,
    is_owner,
    is_yolo_mode,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
    slack_cfg,
)
from kiro_crew.slack.interactions import dispatch as dispatch_interactive
from kiro_crew.slack.sessions_view import (
    _HOME_TAB_SESSIONS_PER_KIND,
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_TASKRUNNER,
    _build_sessions_blocks,
    _collect_recent_sessions_off_loop,
    _message_surface_limit,
    sessions_include_ended,
)
from kiro_crew.slack.transport_dispatch import flat_dm_session_key, handle_message_transport
from kiro_crew.start_priority import person_priority
from kiro_crew.stats import Stats
from kiro_crew.transcribe import audio_exceeds_secs, batch_duration_cap_secs
from kiro_crew.transcribe import is_available as stt_available
from kiro_crew.transcribe import load_stt_config, transcribe_audio

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Socket Mode setup
# ---------------------------------------------------------------------------


def _wire_slack_code_capability_probe(
    orch: GatewayOrchestrator, web_client: AsyncWebClient
) -> None:
    """Re-read the install's Slack Code capability on every socket (re)connect.

    Slack sends ``{"type": "hello"}`` each time a Socket Mode connection opens,
    so a message listener that answers it covers the first connect and every
    reconnect. The probe runs as its own task: the listener must not hold the
    socket's receive loop for an HTTP round trip.
    """
    from kiro_crew.slack import features, scope_probe

    state = orch.dashboard_state
    features.set_capability_notifier(
        scope_probe.capability_notifier(state.notify if state is not None else None)
    )
    tasks: set[asyncio.Task[None]] = set()

    async def _on_message(_client: Any, message: dict, _raw: Any = None) -> None:
        if not isinstance(message, dict) or message.get("type") != "hello":
            return
        task = asyncio.create_task(scope_probe.refresh_capabilities(web_client))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    listeners = getattr(orch._socket_client, "message_listeners", None)
    if isinstance(listeners, list):
        listeners.append(_on_message)


async def init_socket_mode(orch: GatewayOrchestrator, seen: SeenCache) -> None:
    """Wire up the Socket Mode client and attach the event listener.

    Does nothing when Slack is disabled (missing tokens or no allowed
    users).  Mutates ``orch._socket_client`` in place.

    Awaited on the gateway loop, never offloaded whole: constructing
    ``WSSocketModeClient`` requires a current event loop in the constructing
    thread (its ``__init__`` ends in ``asyncio.ensure_future``), so running
    this function in a worker thread crashes every Slack-enabled boot with
    ``RuntimeError: There is no current event loop``.  The two blocking calls
    it contains — the YOLO grant's profiles-dir walk and the enterprise
    ``auth.test`` network call — are offloaded individually below instead,
    which keeps the security-relevant early-return ordering (owner check,
    then YOLO grant, then enterprise validation) intact.
    ``test_slack_events_coverage.py::TestInitSocketMode`` pins both halves.
    """
    if not orch._slack_enabled:
        return

    if not orch._owner_id:
        logger.error("KIROCREW_OWNER_ID is not set — Slack disabled for security")
        orch._slack_enabled = False
        orch.slack = None
        return

    # Invariant: _allowed_users contains only the owner (multi-user disabled)
    assert orch._owner_id, "owner_id must be set"

    # Share owner-only allowlist and tracking channels with handler modules
    set_allowed_users(orch._allowed_users)
    set_tracking_channels(orch._tracking_channels)
    set_open_channels(orch._open_channels)
    set_owner_id(orch._owner_id)
    if orch._cfg.agent.dangerously_skip_permissions:
        # grant_declared_yolo walks the profiles dir — blocking, so off-loop.
        await asyncio.to_thread(set_yolo_mode, True)
    set_orch_cfg(orch._cfg)
    if orch.dashboard_state:
        set_dashboard_state(orch.dashboard_state)

    # ── Enterprise Grid workspace validation ──
    # Blocks data exfiltration via personal/external Slack workspaces.
    extra_ids = orch._cfg.slack_enterprise_ids
    # Route through the active PlatformContext's Slack enterprise gate.  The
    # Default gate is open (opt-in allowlist), identical to today; the Amazon
    # companion supplies a fail-closed workspace allowlist.  validate_enterprise
    # does a synchronous auth.test network call — blocking, so off-loop.
    _validate = current_context().slack_gate.validate_enterprise
    if not await asyncio.to_thread(_validate, orch._bot_token, extra_ids=extra_ids):
        logger.error("Slack workspace failed enterprise validation — Slack disabled")
        orch._slack_enabled = False
        orch.slack = None
        return

    web_client = AsyncWebClient(token=orch._bot_token)
    orch._socket_client = WSSocketModeClient(
        app_token=orch._app_token,
        web_client=web_client,
    )
    _wire_slack_code_capability_probe(orch, web_client)

    async def _on_event(client: WSSocketModeClient, req: SocketModeRequest) -> None:
        # Reserve before the ACK's first suspension. A paused update returns
        # without acknowledging, so Slack retries the whole envelope on the new
        # gateway instead of accepting a card/command that this process cannot
        # durably finish. The Socket Mode SDK runs this listener in one task per
        # envelope, so task-lifetime release covers ACK plus all inline routing.
        if orch.sessions is not None and not await admit_inbound_callback(
            orch.sessions,
            channel_type="slack",
            route=None,
        ):
            logger.info("slack envelope left unacknowledged during update restart")
            return

        # Always ack immediately so Slack doesn't retry
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.debug("Failed to ack event (WebSocket not ready), skipping")
            return

        if req.type == "interactive":
            t = asyncio.create_task(dispatch_interactive(req.payload or {}))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type == "slash_commands":
            payload = req.payload or {}
            t = asyncio.create_task(_handle_slash(orch, payload))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type != "events_api":
            return

        event = (req.payload or {}).get("event", {})
        event_type = event.get("type")

        # ── Tracking-channel join → allowlist prompt ──
        if event_type == "member_joined_channel":
            _maybe_prompt_owner(orch, event)
            return

        # ── Home Tab ──
        if event_type == "app_home_opened":
            user = event.get("user")
            if event.get("tab") == "home" and user:
                if is_allowed_user(user):
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="allowed",
                        source="slack",
                    )
                    task = asyncio.ensure_future(_publish_home_tab(orch, user))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="denied",
                        source="slack",
                        error="unauthorized sender",
                    )
            return

        # ── Agent sessions: native Stop and user renames (agent_sessions) ──
        if event_type in AGENT_SESSION_EVENTS:
            dispatch_agent_session_event(orch, req.payload or {}, event)
            return

        # ── Messages and mentions ──
        if event_type not in ("message", "app_mention"):
            return
        _bot_id = event.get("bot_id")
        _subtype = event.get("subtype")
        # ── message_deleted: cancel queued or in-flight messages ──
        if _subtype == "message_deleted":
            await _handle_message_deleted(orch, event)
            return
        # A bot-authored event is admitted ONLY on a positive match of its
        # bot_id against the slack.trusted_bot_ids allowlist. The rule itself
        # (deny by default, own id never trusted, unverified self id fails
        # closed) lives in ONE place — trusted_bot_admission — which the
        # transport applies too, so one owner keeps both drop sites in step.
        #
        # The admission is carried as from_trusted_bot so _route_message lets
        # the bot_id stand in as sender_id and handle_message suppresses error
        # replies (echo-loop guard). Successful-reply loops are bounded by the
        # per-thread turn cap in _route_message (slack.trusted_bot_turn_limit);
        # richer cross-bot coordination is the agent layer's job (envelope
        # protocol).
        #
        # READ TIMING: this site passes the LIVE config, so an operator's
        # allowlist edit takes effect on the next event without a restart. The
        # transport deliberately freezes a snapshot instead; the predicate
        # takes the set as an argument precisely so each site owns that choice.
        #
        # The trust decision runs BEFORE the generic subtype filter because a
        # bot-authored message commonly carries subtype == "bot_message": the
        # untrusted denial must be audited (not silently subtype-dropped), and
        # a trusted bot's bot_message must pass the subtype gate below.
        _from_trusted_bot, _deny_error = trusted_bot_admission(
            _bot_id or "", orch._cfg.slack.trusted_bot_ids
        )
        if _deny_error:
            sel().log_api_access(
                caller=_bot_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                error=_deny_error,
            )
            return
        if _subtype and _subtype != "file_share":
            if not (_from_trusted_bot and _subtype == "bot_message"):
                return

        # Enterprise Grid: envelope team_id is the *bot's* workspace;
        # event["team"] may be the *sender's* workspace in shared channels.
        # Always prefer envelope to prevent cross-workspace bypass.
        outer_team = (req.payload or {}).get("team_id", "")
        if outer_team:
            event["team"] = outer_team
        elif not event.get("team"):
            logger.warning(
                "Enterprise Grid: no team_id from event or envelope " "(sender=%s) — rejecting",
                event.get("user", "unknown"),
            )
            sel().log_api_access(
                caller=event.get("user", "unknown"),
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="missing_team_id",
            )
            return

        await _route_message(
            orch,
            event,
            seen,
            is_mention=(event_type == "app_mention"),
            from_trusted_bot=_from_trusted_bot,
        )

    orch._socket_client.socket_mode_request_listeners.append(_on_event)  # type: ignore[arg-type]
    register_drain(_CHANNEL, lambda session_key: _drain_slack_queue(orch, session_key))


async def _post_stop_notice(orch: Any, channel: str, text: str, ts: str | None) -> str:
    """Post a ``!stop`` reply; ``""`` when the channel policy refuses the write.

    A pure ``!stop`` is exempt from the channel list so a withdrawn channel can
    still halt its turn; its replies are not, and a refused one must not abort
    the stop or the audit that follows it.
    """
    try:
        return await orch.slack.post_message(channel, text, ts)
    except SlackChannelRefused:
        logger.info("!stop notice withheld: channel refused by policy")
        return ""


async def _route_message(
    orch: GatewayOrchestrator,
    event: dict,
    seen: SeenCache,
    is_mention: bool = False,
    from_trusted_bot: bool = False,
) -> None:
    """Validate, dedup, check activation mode, and dispatch an incoming Slack message."""
    sender_id = event.get("user", "") or (event.get("bot_id", "") if from_trusted_bot else "")
    channel = event.get("channel", "")
    text = _string_field(event, "text")
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts", "")
    team_id = event.get("team", "")
    files = event.get("files", [])

    # Slack forwards carry content in attachments, not text — recover it so the
    # forward isn't silently dropped by the (not text and not files) guard below.
    # A positively admitted trusted bot may also carry card text in an ordinary
    # attachment; keep that recovery unavailable to every other sender.
    if not text or text in _SLACK_BLOCK_FALLBACKS:
        fallback = "" if text in _SLACK_BLOCK_FALLBACKS else text
        text = _extract_shared_text(event)
        if not text and from_trusted_bot:
            text = _extract_trusted_bot_attachment_text(event)
        text = text or fallback
    # Pasted tables live only in blocks: append them even when text is present.
    table_text = _extract_table_text(event)
    if table_text and table_text not in text:
        text = f"{text}\n\n{table_text}" if text else table_text

    logger.debug("Stream debug: team_id=%s user_id=%s channel=%s", team_id, sender_id, channel)

    if not sender_id or not channel or (not text and not files):
        return

    # ── Enterprise origin check: reject messages from swapped tokens ──
    # Per-message gate via the active PlatformContext (default-open; Amazon
    # companion fail-closed).
    if not current_context().slack_gate.check_message_origin(team_id):
        logger.error("Message rejected: team_id=%s does not match validated workspace", team_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            resources=f"team_id={team_id} channel={channel}",
            error="enterprise_origin_mismatch",
        )
        return

    # ── Workspace routing cache for org-wide installs ──
    # Slack Web API calls (chat.postMessage, chat.startStream, etc.) need
    # team_id when the bot is org-wide installed, and the value must be the
    # workspace the CHANNEL lives in. ``event.team`` is the AUTHOR's
    # workspace — a participant's, not the channel's, on a Slack Connect
    # shared channel — so it must never seed this cache; resolve the home
    # workspace from conversations_info instead (cached per process).
    ensure_home_team = getattr(orch.slack, "ensure_channel_team", None)
    if ensure_home_team is not None:
        # The seam is duck-typed on purpose: orch.slack may be the real
        # client, a test double, or a wrapper that implements this hook
        # synchronously or as a coroutine. Accept either; only an awaitable
        # is awaited.
        outcome = ensure_home_team(channel)
        if inspect.isawaitable(outcome):
            await outcome

    # ── Access control: record authorization decision early for SEL audit ──
    # The ephemeral rejection is deferred until after activation checks so
    # users in observe/mention channels aren't spammed, but the SEL event
    # is always emitted to preserve the audit trail.
    # A trusted bot (from_trusted_bot=True) was already positively matched
    # against the slack.trusted_bot_ids allowlist at the drop site; that
    # allowlist IS its authorization — a bot_id is never in the owner-only
    # user allowlist — so it is granted access equivalent to an allowed user.
    # The audit records the decision basis so trusted-bot admissions stay
    # traceable. TWO exceptions fail closed:
    # 1. Review-mode channels: review mode delivers the draft via an
    #    ephemeral to the sender, and chat.postEphemeral requires a human
    #    user id — a bot_id target fails, so the turn would run with an
    #    undeliverable reply. Matches the transport-path gate that also
    #    excludes review mode.
    # 2. Turn cap: a thread that has already run slack.trusted_bot_turn_limit
    #    consecutive trusted-bot turns admits no more until an allowed human
    #    posts in it. Without the cap, two mutually trusted gateways admit
    #    each other's replies as fresh turns indefinitely (unbounded
    #    successful-reply loop); the count is per thread and resets on a
    #    processed human message, so legitimate mesh exchanges keep working
    #    under human supervision.
    _thread_key = f"{channel}:{thread_ts or msg_ts}"
    _turn_capped = from_trusted_bot and _trusted_bot_turns.count(_thread_key) >= max(
        1, slack_cfg(orch).slack.trusted_bot_turn_limit
    )
    _owner_authorized = is_allowed_user(sender_id)
    _trusted_bot_admitted = (
        from_trusted_bot
        and not _turn_capped
        and slack_cfg(orch).channel_config(channel).activation != ACTIVATION_REVIEW
    )
    _user_authorized = _owner_authorized or _trusted_bot_admitted
    if _user_authorized:
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="allowed",
            source="slack",
            resources="" if _owner_authorized else "trusted_bot",
        )
    else:
        logger.warning("Ignoring message from unauthorized user %s", sender_id)
        if not from_trusted_bot:
            _deny_error = "unauthorized sender"
        elif slack_cfg(orch).channel_config(channel).activation == ACTIVATION_REVIEW:
            _deny_error = "trusted_bot_denied_in_review_channel"
        else:
            _deny_error = "trusted_bot_turn_limit_reached"
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            error=_deny_error,
        )

    # ── Message-interceptor seam (Default: PROCESS = inline, OSS-identical) ──
    # An edition may intercept the message here and turn it into an out-of-band
    # challenge-redirect (e.g. a presigned dashboard-session link) instead of
    # processing it inline. The public DefaultSlackEnterpriseGate ALWAYS returns
    # PROCESS and cannot raise, so the standalone build falls straight through
    # exactly as before — the fallback below is never reached in standalone.
    #
    # ORDERING (security-critical): this runs BEFORE any content is recorded or
    # processed — the observe-mode channel_history.push below, audio transcription,
    # image/file download, and the non-observe history push all follow. An
    # unverified sender's message content (prompt-injection text, attachments) must
    # NOT be persisted to channel history before the gate decides: otherwise a
    # later VERIFIED turn in the same channel could pull that stored content into
    # agent context, bypassing the very challenge gate the edition relies on. So
    # the interceptor is the first thing after the user-allowlist check, before
    # the message leaves a trace. It keys on (sender_id, channel) identity, so it
    # does not need the post-transcription/mention-stripped text; it gets the raw
    # text for challenge context/logging only.
    #
    # The fallback is DROPPED (deny-by-default), NOT PROCESS: this branch is only
    # reached when a COMPOSED gate raised a transient error (a
    # PlatformCompositionError is re-raised by safe_context_call, never degraded).
    # A composed edition installs its interceptor precisely to keep unverified
    # traffic away from the agent, so an erroring interceptor must fail CLOSED —
    # degrading to PROCESS would let a message bypass the gate (deny-by-default).
    #
    # Gated on _user_authorized: an UNauthorized sender is not a challenge
    # candidate (they are rejected with an ephemeral below) and the observe-mode
    # history push is itself _user_authorized-gated, so only an authorized sender's
    # content can be recorded pre-gate — that is exactly the leak this ordering
    # closes. Skipping the interceptor for unauthorized senders also preserves
    # their existing ephemeral-rejection UX unchanged.
    if _user_authorized:
        _intercept_clean = text
        if is_mention and text.startswith("<@"):
            _end = text.find(">")
            if _end != -1:
                _intercept_clean = text[_end + 1 :].lstrip()
        # Interceptor-specific dedup: Slack re-delivers the same event on ack
        # timeout, and this seam runs BEFORE the main _route_message dedup (which
        # must stay after the activation check). Without a guard here a redirecting
        # adapter would re-mint + re-post a challenge link on every retry. Key it in
        # a separate "intercept:" namespace and RECORD it only on a non-PROCESS
        # decision (below) — never for PROCESS, so a passed message still reaches
        # the main dedup and the paired app_mention/message dual-event is preserved.
        _intercept_seen_key = f"intercept:{msg_ts}"
        if seen.check(_intercept_seen_key):
            # A prior delivery of this event already got a challenge/deny verdict;
            # drop the retry silently (the original already replied / was audited).
            return
        _decision = safe_context_call(
            lambda: current_context().slack_gate.intercept_message(
                orch,
                channel=channel,
                sender_id=sender_id,
                clean_text=_intercept_clean,
                thread_ts=thread_ts,
                msg_ts=msg_ts,
            ),
            fallback=InterceptDecision.DROPPED,
            log_message="slack_gate.intercept_message failed; failing closed (DROPPED)",
        )
        if _decision is not InterceptDecision.PROCESS:
            # REDIRECTED (a challenge was issued) or DROPPED (denied / gate error):
            # the gate owns any user-facing reply; short-circuit before ANY content
            # is recorded/processed. No image temps have been downloaded yet at this
            # point (that happens further below), so nothing to clean up here.
            seen.add(_intercept_seen_key)  # dedup subsequent retries of this event
            # SEL audit: the interceptor is a permission decision distinct from the
            # earlier allowlist check, so its verdict MUST reach the audit trail
            # (backend-security-controls). REDIRECTED = a challenge was issued;
            # DROPPED = denied or a fail-closed gate error.
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message.intercept",
                outcome="denied",
                source="slack",
                resources=channel,
                error=f"intercept={getattr(_decision, 'value', _decision)}",
            )
            logger.info(
                "Message from %s in %s intercepted (%s)",
                sender_id,
                channel,
                getattr(_decision, "value", _decision),
            )
            return

    # ── Channel activation mode (checked BEFORE ephemeral & dedup) ──
    # When activation=mention, Slack sends both a `message` and an
    # `app_mention` event for the same msg_ts.  We must skip the plain
    # `message` event *without* marking it as seen so the subsequent
    # `app_mention` event is still processed.
    ch_cfg = slack_cfg(orch).channel_config(channel)
    activation = ch_cfg.activation

    if activation == ACTIVATION_OFF:
        # Allow !channel commands through so the owner can re-enable the channel.
        # Text may start with "<@BOTID> " when @mentioned, so strip that first.
        _stripped = text.lstrip()
        if _stripped.startswith("<@"):
            end = _stripped.find(">")
            if end != -1:
                _stripped = _stripped[end + 1 :].lstrip()
        if not _stripped.startswith("!channel"):
            logger.debug("Channel %s activation=off — ignoring message", channel)
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="activation=off",
            )
            return

    # ── Inbound channels-governance gate (off-loop) — BEFORE any side effect ──
    # A ``channels`` policy that denies ``slack`` must stop inbound processing
    # before _route_message does anything observable: the observe-mode and
    # non-observe ``channel_history.push`` (denied content must not be recorded,
    # or a later ALLOWED turn in the channel could pull it into agent context),
    # sender display-name lookups, audio transcription, image/file downloads, the
    # ``!restart`` bang alias (a gateway restart), and session queueing/dispatch.
    # The gate inside handle_message() runs too late for this path — every one of
    # those side effects precedes it — so we gate HERE, right after auth /
    # interceptor / activation-off and before the first side effect.
    #
    # EXEMPT only cancellation (``!stop``): a denied channel must still be able to
    # halt a runaway session it previously started. ``!restart`` is NOT
    # cancellation and stays gated. Default OSS build (no ``channels`` policy)
    # permits. handle_message keeps its own
    # gate as defense-in-depth for its other entry points (interaction
    # re-dispatch, synthetic sends).
    #
    # The exemption requires a PURE cancellation: text is exactly ``!stop`` AND
    # there are NO attachments. A ``!stop`` message carrying files/voice is not a
    # real cancellation — the transcription/attachment injection below rewrites
    # ``text`` so the downstream ``clean_text == "!stop"`` intercept no longer
    # matches — and exempting it would let the denied channel still DOWNLOAD the
    # attachment, TRANSCRIBE the voice memo, and PUSH the content to
    # channel_history (the very leak this gate prevents) before any turn is
    # blocked. So an attachment-bearing ``!stop`` is gated like any other message.
    _gate_text = text
    if is_mention and _gate_text.startswith("<@"):
        _gt_end = _gate_text.find(">")
        if _gt_end != -1:
            _gate_text = _gate_text[_gt_end + 1 :].lstrip()
    _is_pure_stop = _gate_text.strip().lower() == "!stop" and not files
    if not _is_pure_stop:
        if not await channel_inbound_permitted("slack"):
            logger.info("slack inbound dropped: denied by channels governance policy")
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="channels governance policy",
            )
            return
        # The channel itself: channels.posture.slack.allowed_channel_ids. In an
        # enterprise build no channel is admitted until the policy names the list.
        # A pure ``!stop`` stays exempt for the reason above.
        from kiro_crew.slack.channel_policy import channel_refusal

        # Audited just below with the sender, so not a second time inside.
        channel_refused = channel_refusal(channel, audit=False)
        if channel_refused:
            logger.info("slack inbound dropped: %s", channel_refused)
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="channel_not_allowed_by_policy",
            )
            return

    # Resolve sender's Slack display name so the LLM uses the actual
    # profile name instead of guessing from memory. Cached on channel
    # history (for history context) and passed to handle_message.
    _sender_display: str | None = None
    if orch.channel_history:
        _sender_display = orch.channel_history._user_names.get(sender_id)
    if not _sender_display and orch.slack and hasattr(orch.slack, "get_user_info"):
        try:
            info = await orch.slack.get_user_info(sender_id)
            _sender_display = info.get("real_name") or sender_id
            if orch.channel_history:
                orch.channel_history.set_user_name(sender_id, _sender_display)
        except Exception:
            logger.debug("Failed to resolve display name for %s", sender_id, exc_info=True)

    # Fallback: if display name is still the raw Slack ID, resolve from
    # allowed_users config (works even without Slack users:read scope).
    if (not _sender_display or _sender_display == sender_id) and hasattr(orch, "_cfg"):
        for u in getattr(orch._cfg.slack, "allowed_users", []):
            if u.get("slack_id") == sender_id and u.get("name"):
                _sender_display = u["name"]
                if orch.channel_history:
                    orch.channel_history.set_user_name(sender_id, u["name"])
                break

    # Observe mode: record history from authorized users only, so non-owner
    # messages cannot influence LLM context.
    if activation == ACTIVATION_OBSERVE:
        if should_record_observe_history(orch.channel_history, _user_authorized):
            assert orch.channel_history is not None  # narrowed by helper
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)
        if not is_mention and not _thread_follow_admits(
            orch,
            thread_follow=ch_cfg.thread_follow,
            activation=activation,
            thread_ts=thread_ts,
            text=text,
            sender_id=sender_id,
            channel=channel,
        ):
            return

    if activation in (ACTIVATION_MENTION, ACTIVATION_REVIEW) and not is_mention:
        # In mention/review mode: ignore messages without @mention UNLESS the
        # message is a reply in a thread where the bot already has an active
        # session (i.e., the bot was already @mentioned in that thread) and is
        # not addressed to someone else. When thread_follow=false, always require
        # @mention even in active threads.
        if not _thread_follow_admits(
            orch,
            thread_follow=ch_cfg.thread_follow,
            activation=activation,
            thread_ts=thread_ts,
            text=text,
            sender_id=sender_id,
            channel=channel,
        ):
            return

    # ── Access control: send ephemeral rejection ──
    # Only reached for messages the bot would actually respond to,
    # preventing notification spam in observe/mention channels.
    if not _user_authorized:
        if orch.slack:
            try:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "⛔ You are not authorized to use this bot. "
                    "Ask the owner to add you to the allowlist.",
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # Dedup AFTER activation check — prevents the plain `message` event
    # from poisoning the cache before the `app_mention` event arrives.
    if seen.check_and_add(msg_ts):
        return

    # What the user sent, captured BEFORE transcription and file ingestion
    # rewrite ``text``. The shutdown spool records this, never the ingested form,
    # which inlines turn-owned temp paths (dead after a restart) and extracted
    # file contents -- the ``InboundRoute.text`` contract that Weixin, WeCom and
    # WhatsApp already keep. It reuses ``_gate_text``, the mention-stripped
    # message captured above before transcription and ingestion.
    _spool_text = _gate_text
    _spool_attachments = len(files) if files else 0

    # ── Transcribe audio files (voice memos) ──
    # Placed after dedup + auth to avoid expensive work on duplicate events
    # or unauthorized users.
    _attachment_temp_paths: list[str] = []
    _had_voice_input = False
    if files and orch.slack and _user_authorized:
        memos = [f for f in files if is_voice_memo(f)]
        if memos:
            transcripts: list[str] = []
            # Decided once per message, not per memo: an unusable transcriber
            # cannot become usable between two attachments of one message.
            #
            # Off the loop, like every other caller of this: on the `local` provider it
            # reaches the availability probe, which imports the recogniser binding and
            # dlopens a native library (measured at 209 ms cold). Inline, the first
            # inbound voice memo of a boot stalled the gateway's loop and its liveness
            # heartbeat with it.
            stt_ok = await asyncio.to_thread(stt_available)
            if stt_ok:
                transcripts = await _transcribe_with_reaction(
                    orch.slack,
                    channel,
                    msg_ts,
                    orch,
                    files,
                )
                if transcripts:
                    raw = "\n".join(transcripts)
                    raw, _ = redact_exfiltration_urls(raw)
                    raw, _ = redact_credentials(raw)
                    prefix = f"[Voice memo transcription]\n{raw}\n[End of transcription]"
                    text = f"{prefix}\n\n{text}" if text else prefix
                    _had_voice_input = True
            text = _voice_memo_context(text, len(memos), len(transcripts), available=stt_ok)

        # ── Process non-audio files (images, text, opaque files, etc.) ──
        attachment_paths, text_blocks = await process_slack_files(orch, files)
        _attachment_temp_paths = attachment_paths

        # Image paths are inlined by ACP; opaque paths remain available to agent tools.
        if attachment_paths:
            paths_text = "\n".join(attachment_paths)
            text = f"{text}\n{paths_text}" if text else paths_text

        # Inject text file contents
        if text_blocks:
            blocks_text = "\n\n".join(text_blocks)
            text = f"{text}\n\n{blocks_text}" if text else blocks_text

    # Bail out if we still have no text after attempting transcription
    if not text:
        for p in _attachment_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        return

    def _cleanup_attachment_temps() -> None:
        for p in _attachment_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    # Record messages in channel history buffer (observe channels already
    # pushed above, so skip them here to avoid duplicates).
    if activation != ACTIVATION_OBSERVE:
        if orch.channel_history is None:
            logger.error("channel_history not initialised — skipping history push")
        else:
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)

    # Strip the leading bot @mention so the LLM sees clean text.
    # app_mention events always start with "<@BOTID> ..." — just slice past the first ">".
    clean_text = text
    if is_mention and text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            clean_text = text[end + 1 :].lstrip()
    if not clean_text:
        _cleanup_attachment_temps()
        return

    # ── !stop: intercept BEFORE handle_message to bypass session semaphore ──
    if clean_text.strip().lower() == "!stop":
        if not (is_owner(sender_id) or is_allowed_user(sender_id)):
            sel().log_api_access(
                caller=sender_id,
                operation="slack.stop_command",
                outcome="denied",
                source="slack",
                resources="!stop",
                error="unauthorized sender",
            )
            if orch.slack:
                await orch.slack.post_message(channel, "⛔ Not authorized.", thread_ts or msg_ts)
            return
        if not orch.sessions:
            sel().log_tool_invocation(
                session_key=thread_ts or msg_ts,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await _post_stop_notice(orch, channel, "Nothing running.", thread_ts or msg_ts)
            return
        _flat_stop_key = flat_dm_session_key(
            channel, thread_ts, enabled=_dm_single_session_enabled(orch, channel)
        )
        # A dashboard-linked thread wins over the flat key. When !stop is typed
        # inside a DM thread that a dashboard send-to-Slack owns, the running
        # turn lives under THAT owner (keyed by thread_ts in the thread index),
        # not under the channel-scoped flat key -- so stopping the flat key would
        # leave the linked turn's provider running while acking a session that
        # was never busy. A SELF-DERIVED owner (``slack:<thread_ts>``, the
        # per-thread session the flat feature merges away) is not a real binding
        # and is ignored, matching handle_message_transport's _resolve_thread_owner.
        _linked_owner: str | None = None
        if _flat_stop_key and thread_ts:
            _owner = orch.sessions.get_session_for_thread(thread_ts)
            # A SELF-DERIVED owner (``slack:<thread_ts>``, the per-thread session
            # the flat feature merges away) is not a real binding and is ignored,
            # matching handle_message_transport's _resolve_thread_owner. Any OTHER
            # owner is a real dashboard binding that must keep the stop.
            if _owner is not None and _owner != canonical_key(thread_ts):
                _linked_owner = _owner
        if _linked_owner is not None:
            session_key = _linked_owner
            stop_post_ts: str | None = thread_ts
        else:
            session_key = _flat_stop_key or (thread_ts or msg_ts)
            # Where the acknowledgements go. session_key is only a Slack timestamp
            # while the session is thread-scoped; a single-session DM keys by
            # channel, and passing that as thread_ts would be rejected. A flat DM
            # therefore acks where the !stop was typed -- inside its thread if it
            # had one, at channel root otherwise -- the same split the turn uses.
            stop_post_ts = thread_ts if _flat_stop_key else session_key
        # Recorded BEFORE the liveness checks: a turn between its abandoned
        # attempt and its compaction replay has no session at this moment, and
        # an interaction-originated turn has no registered task either; the
        # replay reads this record to stay dropped (``note_user_stop``).
        # Against the thread's OWNING session, not the bare thread key: a
        # linked thread's turns -- and their replay -- run under the dashboard
        # session that owns it, and that is the key the replay reads. For a flat
        # DM session_key is already the channel-scoped owning key, so the lookup
        # falls back to it unchanged.
        force_stop = False
        if _compaction_in_flight(orch.sessions, session_key):
            # A repeat !stop within the window is the second press and forces
            # (the Kill Now button is the other route). The first is declined
            # BEFORE any side effect: the Stop record, the queue clear, the
            # pending-file unlink and the task pop below all assume the turn is
            # being ended, and a Stop the session's own /compact turn declines
            # ends nothing. Same answer ``stop_turn`` gives for the race. Keyed
            # by the presser too: a thread's session key is every member's, and
            # another member's declined !stop must not arm this member's first.
            force_stop = consume_stop_declined(session_key, sender_id)
        if _compaction_in_flight(orch.sessions, session_key) and not force_stop:
            # Posted before the marker is armed, and nothing is armed when there
            # is no client to post with: an undelivered warning plus an armed
            # escalation is a retry that hard-resets the session with this member
            # never told that it would.
            async def _say_declined() -> bool:
                if not orch.slack:
                    return False
                # The post hands back the ts of what landed, so a falsy one is a
                # warning this member never saw and must arm nothing.
                return bool(
                    await _post_stop_notice(
                        orch, channel, STOP_DECLINED_COMPACTING_TEXT, stop_post_ts
                    )
                )

            await decline_stop(session_key, sender_id, _say_declined)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="compacting",
                metadata={"user": sender_id, "channel": channel},
            )
            return
        note_user_stop(orch.sessions, orch.sessions.get_session_for_thread(session_key) or session_key)
        has_session = orch.sessions.has_session(session_key)
        # READ, not popped: the task is removed only once the cancel is known
        # to have gone through, below.
        active_task = orch._session_tasks.get(session_key)
        if has_session or active_task:
            # What Stop is asked to drop is what was queued WHEN IT WAS PRESSED,
            # and it must neither START nor be lost while the stop is in flight:
            # the cancelled turn's end-of-turn drain would otherwise dispatch it
            # during the awaits below. So it is DETACHED here, before the first
            # await (files kept), and either dropped once the stop went through
            # or put back if the stop is declined. A message admitted after this
            # line is newer intent and is never touched.
            queued_at_press = orch.sessions.detach_queue(session_key)
            pending_at_press = list(orch._pending_queue.pop(session_key, None) or ())
            # Ordering contract: from here until the last overlapping Stop has
            # settled, ``_key_busy`` reads the key as busy, so neither turn-end
            # drain (``_on_done``, ``_on_transport_done``) nor ``_drain_slack_queue``
            # starts a queued message that a ``stop_turn`` would then cancel. What
            # each Stop detached waits in the shared hold until then, and the last
            # release puts it back (``_settle_stop_hold``) and always runs the
            # drain: the running turn may have ended meanwhile, or never been
            # registered here at all (an interaction-started turn), and the kept
            # entries would otherwise have no dispatcher.
            _hold = _stops_in_flight.setdefault(orch, {}).setdefault(session_key, _StopHold())
            _hold.count += 1
            _held: tuple[list[Any], list[Any]] = (list(queued_at_press), pending_at_press)
            _held_at = len(_hold.held)
            _hold.held.append(_held)
            _released: list[bool] = []

            async def _end_stop_in_flight() -> None:
                if _released:
                    return
                _released.append(True)
                _hold.count -= 1
                if _hold.count > 0:
                    return
                _stops_in_flight.get(orch, {}).pop(session_key, None)
                await _settle_stop_hold(orch, session_key, _hold)
                await _drain_slack_queue(orch, session_key)

            try:
                try:
                    # Post ephemeral "Stopping…" block with Kill Now button
                    if orch.slack:
                        try:
                            await orch.slack.post_ephemeral(
                                channel,
                                sender_id,
                                "Stopping…",
                                blocks=build_stopping_blocks(session_key),
                                thread_ts=stop_post_ts,
                            )
                        except SlackChannelRefused:
                            # A channel the policy refuses still gets its
                            # stop; only the notice is withheld.
                            logger.info("!stop notice withheld: channel refused by policy")

                    async def _on_soft() -> None:
                        if orch.slack:
                            await orch.slack.post_message(
                                channel, "⏹ Execution stopped.", stop_post_ts
                            )

                    async def _on_hard() -> None:
                        if orch.slack:
                            await orch.slack.post_message(
                                channel, "⛔ Execution stopped — session reset.", stop_post_ts
                            )

                    # ``force`` only when set: the default call shape is what every
                    # existing caller and test double of ``stop_turn`` expects.
                    # ``preserve_queue``: what this Stop drops was DETACHED above and
                    # is cleared by identity below; ``stop_turn``'s own whole-queue
                    # clear would take a message admitted since the detach, which is
                    # newer intent this Stop was never aimed at.
                    _kw: dict[str, Any] = {"force": True} if force_stop else {}
                    outcome = await orch.sessions.stop_turn(
                        session_key, preserve_queue=True, on_soft=_on_soft, on_hard=_on_hard, **_kw
                    )
                except BaseException:
                    # The detached work is held only in these locals. If the ephemeral
                    # post or the stop itself raises, nothing below runs: put the work
                    # back where it was, then propagate. Without this a rate-limited
                    # Slack reply silently emptied the user's queue and leaked its
                    # staged attachment files. A forced stop whose reset raised AFTER
                    # popping the session leaves no queue to put it back on; there the
                    # handles' files are unlinked rather than leaked.
                    # It stays in the hold, which puts it back: on the session, or, with
                    # no session left, handed to the successor the hard stop respawns
                    # or parked for a later start, the way the other channels' forced
                    # stop keeps co-tenants' work.
                    raise
                if outcome == "compacting":
                    # The pre-check above passed and a compaction committed during
                    # the ephemeral post. ``stop_turn`` is the authority: nothing was
                    # stopped, so what was detached goes back (through the hold),
                    # ahead of anything admitted since, and the task stays tracked.
                    # Armed here as on the pre-check decline: the reply promises
                    # that a repeat forces, so the repeat must find a marker -- and
                    # only after the reply landed, since an escalation the member was
                    # never warned about is a silent reset of their session.

                    async def _say_declined_race() -> bool:
                        if not orch.slack:
                            return False
                        # The ts of what landed: a falsy one is a warning this
                        # member never saw and must arm nothing.
                        return bool(
                            await _post_stop_notice(
                                orch, channel, STOP_DECLINED_COMPACTING_TEXT, stop_post_ts
                            )
                        )

                    await decline_stop(session_key, sender_id, _say_declined_race)
                else:
                    # The destructive half, AFTER the outcome: a Stop that ended a
                    # turn drops what was queued behind it. Placed before the cancel
                    # this ran on a declined Stop too and discarded queued work.
                    # Pop only the task this Stop ended. A message admitted during
                    # the awaits above can already be running as a SUCCESSOR entry;
                    # that is the user's newer intent and is left alone, tracked.
                    if orch._session_tasks.get(session_key) is active_task:
                        orch._session_tasks.pop(session_key, None)
                    if force_stop and outcome == "hard":
                        # The forced repeat on a compacting session: the hard reset
                        # popped the session and its queue. The Stop was aimed at the
                        # compaction, not at the queue, so nobody's entries are
                        # dropped, the presser's included. Everything
                        # detached at the press is carried to the successor, as the
                        # other channels' forced stop carries co-tenants' entries
                        # (``force_stop_keeping_others``). Pending (pre-session)
                        # entries go back to their stash for the same reason.
                        _held[0].clear()
                        await hand_queue_to_successor(orch.sessions, session_key, queued_at_press)
                    elif outcome == "hard":
                        # An escalated stop reset the session: the queue goes for
                        # everybody, with every detached entry's files.
                        orch.sessions.clear_queue(session_key, only=queued_at_press)
                        for _item in pending_at_press:
                            unlink_queued_temp_paths(_item[2])
                        _held[0].clear()
                        _held[1].clear()
                    else:
                        # The CALLER's queued messages, not the session's. Every member
                        # of a thread queues under this one session key, so dropping
                        # everything detached would discard messages the other members
                        # are still owed an answer to. This Stop's owner joins the hold's
                        # drop set: the last release drops every stopping member's
                        # entries, whichever Stop detached them, and puts the rest back.
                        # Entries no Slack producer tagged (a dashboard-linked session's
                        # own queue) are nobody's to drop and go back too; the running
                        # turn is still stopped whoever it belongs to.
                        _owner = _entry_owner(sender_id, channel)
                        _hold.dropping[_owner] = max(_hold.dropping.get(_owner, -1), _held_at)
                    if active_task and not active_task.done():
                        active_task.cancel()
            finally:
                # The ONE release, on every path out of the stop: a hold left
                # behind would read the key as busy forever.
                await _end_stop_in_flight()
            # If stop_turn returned "idle" (no active turn), neither callback
            # fired — dismiss the stale "Stopping…" ephemeral explicitly.
            if outcome == "idle" and orch.slack:
                await _post_stop_notice(orch, channel, "Nothing running.", stop_post_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome=outcome,
                metadata={"user": sender_id, "channel": channel},
            )
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await _post_stop_notice(orch, channel, "Nothing running.", thread_ts or msg_ts)
        return

    # ── !restart: bang alias for /kirocrew restart — intercept here so it
    #    never reaches the LLM session. Delegates to the slash handler
    #    (_handle_restart) which owns owner-check + supervisor guard, keeping
    #    a single source of truth for the restart logic. ──
    if clean_text.strip().lower() == "!restart":
        async def _restart_respond(text: str, **_kw: Any) -> None:
            if orch.slack:
                await orch.slack.post_message(channel, text, thread_ts or msg_ts)

        await _handle_restart(orch, sender_id, "", _restart_respond)
        return

    agent_override = ch_cfg.agent or None

    # ── Trusted-bot turn ledger (loop guard bookkeeping) ──
    # Counted HERE — after auth, activation, dedup, the empty-clean_text
    # return, and the !stop/!restart interceptions — so only a message that
    # will actually dispatch (or enqueue) a turn moves the count: a Slack
    # retry, a message/app_mention duplicate pair, an activation-dropped
    # message, a mention with no text after the <@…> strip, or an intercepted
    # command must not burn the thread's budget. Every message reaching this
    # point is authorized, so the non-trusted case is an allowed human:
    # reset. An enqueued turn counts at enqueue time; the rare queued turn
    # later cancelled via message_deleted leaves a one-turn over-count in the
    # fail-closed direction, cleared by the next human message.
    if from_trusted_bot:
        _trusted_bot_turns.increment(_thread_key)
    else:
        _trusted_bot_turns.reset(_thread_key)

    logger.info(
        "Message from %s in %s (activation=%s): %s",
        sender_id,
        channel,
        activation,
        _safe_log(text[:80]),
    )

    # ── Queue check: if session is busy, enqueue instead of blocking ──
    # Keyed on the SAME session the turn will run under. A single-session DM keys
    # by channel, so this has to derive it the same way the turn does -- keyed on
    # the message ts instead, a second DM would read as not-busy, skip the queue,
    # and block inside get_or_create with none of the queued-message feedback.
    _dm_single_session = _dm_single_session_enabled(orch, channel)
    session_key = (
        flat_dm_session_key(channel, thread_ts, enabled=_dm_single_session) or thread_ts or msg_ts
    )
    _task_busy = _key_busy(orch, session_key)
    if _task_busy:
        # A task is already running for this session key.  Try the session-level
        # queue first (semaphore-based); fall back to an orchestrator-level
        # pre-session queue when the session object doesn't exist yet.
        _queued = orch.sessions and orch.sessions.enqueue(
            session_key,
            msg_ts,
            clean_text,
            force=True,
            channel=channel,
            thread_ts=thread_ts,
            sender_id=sender_id,
            team_id=team_id,
            agent_override=agent_override,
            user_display_name=_sender_display,
            # Historical key; carries every attachment temp path for cleanup.
            image_temp_paths=list(_attachment_temp_paths),
            from_trusted_bot=from_trusted_bot,
            spool_text=_spool_text,
            spool_attachments=_spool_attachments,
            **_queue_tags(sender_id, channel),
        )
        if not _queued:
            # Session object not created yet — stash on orch._pending_queue,
            # tagged like a session entry so a caller-scoped !stop can tell
            # this sender's entries from the rest here too.
            orch._pending_queue.setdefault(session_key, []).append(
                (
                    msg_ts,
                    clean_text,
                    dict(
                        channel=channel,
                        thread_ts=thread_ts,
                        sender_id=sender_id,
                        team_id=team_id,
                        agent_override=agent_override,
                        user_display_name=_sender_display,
                        image_temp_paths=list(_attachment_temp_paths),
                        from_trusted_bot=from_trusted_bot,
                        spool_text=_spool_text,
                        spool_attachments=_spool_attachments,
                        **_queue_tags(sender_id, channel),
                    ),
                )
            )
        logger.info(
            "Message %s queued for busy session %s (session_obj=%s)", msg_ts, session_key, _queued
        )
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # NOTE: do NOT _cleanup_attachment_temps() here — clean_text references
        # these paths. The historical image_temp_paths queue key transfers cleanup
        # ownership to _dispatch_queued after the turn runs.
        return
    elif orch.sessions and orch.sessions.enqueue(
        session_key,
        msg_ts,
        clean_text,
        channel=channel,
        thread_ts=thread_ts,
        sender_id=sender_id,
        team_id=team_id,
        agent_override=agent_override,
        user_display_name=_sender_display,
        image_temp_paths=list(_attachment_temp_paths),
        from_trusted_bot=from_trusted_bot,
        spool_text=_spool_text,
        spool_attachments=_spool_attachments,
        **_queue_tags(sender_id, channel),
    ):
        logger.info("Message %s queued for busy session %s", msg_ts, session_key)
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        # See the force=True branch above: cleanup is deferred to
        # _dispatch_queued so every queued attachment path remains valid.
        return

    # ── New transport path: route to the messaging abstraction ──
    # When messaging.use_transport is True, drive the turn through
    # SlackTransport → TurnDriver → SlackRenderer instead of the native
    # inline handle_message loop. Default ON in this fork: MessagingConfig
    # and the loader both default use_transport to True and orch._cfg.messaging
    # is always populated (default_factory), so every install takes this path
    # unless it explicitly sets messaging.use_transport=false to opt back into
    # the native path. (KiroCrew has no challenge-redirect path, so this simply
    # replaces the native dispatch when the flag is on.)
    #
    # Review-mode channels are EXCLUDED from the transport path: review mode is
    # a privacy gate (suppress public streaming/output, post an ephemeral draft
    # with approve/edit/cancel for owner sign-off). That machinery lives only in
    # native handle_message; routing review-mode channels through native keeps
    # that guarantee intact rather than risking a partial re-implementation.
    _use_transport = (
        getattr(getattr(slack_cfg(orch), "messaging", None), "use_transport", False) is True
        and activation != ACTIVATION_REVIEW
    )
    if _use_transport:
        t = asyncio.create_task(
            handle_message_transport(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                context_builder=orch.ctx_builder,
                conversation_log=orch.conv_log,
                # Same approval gating as the native path: respects the
                # configured mode + operator YOLO/SafetyOverride TTL, rather
                # than an unconditional auto-approve. Deny-by-default unless
                # auto-approve is explicitly active.
                approval_mode=_resolve_approval_mode(orch),
                # Per-channel agent override (slack.channels.<id>.agent), same
                # as native handle_message's channel_agent, so a channel-pinned
                # agent is honored on the transport path too.
                agent_override=agent_override,
                # Keyword-command services, same as native handle_message, so
                # `sessions`/`spawn`/`run`/`cron` work on the transport path via
                # the shared maybe_handle_keyword_command interceptor.
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                cron_service=orch.cron_svc,
                # Respect the user's phase-reaction setting, same as native
                # handle_message — live-read per message, NOT orch._cfg (which
                # is captured at startup and would make toggle saves inert
                # until restart).
                reactions_enabled=KiroCrewConfig.load().slack.reactions_enabled,
                # Respect slack.show_thinking (surface reasoning as a 💭 reply).
                show_thinking=KiroCrewConfig.load().slack.show_thinking,
                # History consolidation + display-name context, same as native
                # handle_message (parity: don't drop these on the transport path).
                consolidator=orch.consolidator,
                user_display_name=_sender_display,
                # Live gateway state for the session-directive consumer (a
                # monitor directive on a dashboard-owned thread resolves its
                # slot through orch.dashboard_state).
                gateway=orch,
                # Echo-loop guard parity with native handle_message: the
                # transport path suppresses its error reply for trusted-bot
                # messages (a reply is itself a bot-authored event the peer
                # admits, so replying would ping-pong).
                from_trusted_bot=from_trusted_bot,
                dm_single_session=_dm_single_session,
                start_priority=person_priority(not from_trusted_bot),
                spool_text=_spool_text,
                spool_attachments=_spool_attachments,
            )
        )
        _track_router_task(orch, session_key, t, thread_ts)

        def _on_transport_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
            orch._handler_tasks.discard(task)
            if orch._session_tasks.get(session_key) is task:
                del orch._session_tasks[session_key]
            _cleanup_attachment_temps()
            # Drain queue: only if no other task took over this session.
            # Mirrors native _on_done so messages queued while this session was
            # busy aren't stranded when the transport path is the active route.
            try:
                if not _key_busy(orch, session_key) and orch.sessions:
                    _next = orch.sessions.dequeue(session_key)
                    # Fall back to orchestrator-level pending queue (pre-session).
                    if not _next:
                        _pq = orch._pending_queue.get(session_key)
                        if _pq:
                            _next = _pq.pop(0)
                            if not _pq:
                                del orch._pending_queue[session_key]
                    if _next:
                        _q_ts, _q_text, _q_kw = _next
                        _q_t = asyncio.ensure_future(
                            _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                        )
                        _track_router_task(orch, session_key, _q_t, _q_kw.get("thread_ts"))
                        orch._handler_tasks.add(_q_t)
                        _q_t.add_done_callback(_on_transport_done)
            except Exception:
                logger.exception("_on_transport_done drain failed for %s", session_key)

        t.add_done_callback(_on_transport_done)
        orch._handler_tasks.add(t)
        return

    try:
        t = asyncio.create_task(
            handle_message(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                team_id=team_id,
                approval_mode=_resolve_approval_mode(orch),
                context_builder=orch.ctx_builder,
                cron_service=orch.cron_svc,
                conversation_log=orch.conv_log,
                consolidator=orch.consolidator,
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                channel_agent=agent_override,
                user_display_name=_sender_display,
                from_trusted_bot=from_trusted_bot,
                channel_activation=activation,
                had_voice_input=_had_voice_input,
                spool_text=_spool_text,
                spool_attachments=_spool_attachments,
                start_priority=person_priority(not from_trusted_bot),
            )
        )
    except Exception:
        logger.exception("Failed to create handle_message task")
        _cleanup_attachment_temps()
        return

    _track_router_task(orch, session_key, t, thread_ts)

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        orch._handler_tasks.discard(task)
        if orch._session_tasks.get(session_key) is task:
            del orch._session_tasks[session_key]
        _cleanup_attachment_temps()
        # Drain queue: only if no other task took over this session
        try:
            if not _key_busy(orch, session_key) and orch.sessions:
                _next = orch.sessions.dequeue(session_key)
                # Fall back to orchestrator-level pending queue (pre-session messages)
                if not _next:
                    _pq = orch._pending_queue.get(session_key)
                    if _pq:
                        _next = _pq.pop(0)
                        if not _pq:
                            del orch._pending_queue[session_key]
                if _next:
                    _q_ts, _q_text, _q_kw = _next
                    _q_t = asyncio.ensure_future(
                        _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                    )
                    _track_router_task(orch, session_key, _q_t, _q_kw.get("thread_ts"))
                    orch._handler_tasks.add(_q_t)
                    _q_t.add_done_callback(_on_done)
        except Exception:
            logger.exception("_on_done drain failed for %s", session_key)

    orch._handler_tasks.add(t)
    t.add_done_callback(_on_done)

# The Socket Mode wiring (``init_socket_mode``) and the message router
# (``_route_message``) stay here; the rest lives in the ``events_*`` owner modules beside
# this one, lowest layer first. A read of ``kiro_crew.slack.events.<name>`` is served by
# the owner that binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` --
# reaches every owner holding it (:mod:`kiro_crew._facade_mirror`). Owners never import
# this module, and they log as ``kiro_crew.slack.events``. New work goes to the owner of
# its responsibility, listed in the slack-gateway spec.
from kiro_crew.slack.events_block_text import _SLACK_BLOCK_FALLBACKS  # noqa: E402
from kiro_crew.slack.events_block_text import _extract_shared_text  # noqa: E402
from kiro_crew.slack.events_block_text import _extract_table_text  # noqa: E402
from kiro_crew.slack.events_block_text import _extract_trusted_bot_attachment_text  # noqa: E402
from kiro_crew.slack.events_block_text import _string_field  # noqa: E402
from kiro_crew.slack.events_home_tab import _publish_home_tab  # noqa: E402
from kiro_crew.slack.events_inbound import SeenCache  # noqa: E402
from kiro_crew.slack.events_inbound import _background_tasks  # noqa: E402
from kiro_crew.slack.events_inbound import _dm_single_session_enabled  # noqa: E402
from kiro_crew.slack.events_inbound import _handle_message_deleted  # noqa: E402
from kiro_crew.slack.events_inbound import _maybe_prompt_owner  # noqa: E402
from kiro_crew.slack.events_inbound import _thread_follow_admits  # noqa: E402
from kiro_crew.slack.events_inbound import _trusted_bot_turns  # noqa: E402
from kiro_crew.slack.events_queue import _CHANNEL  # noqa: E402
from kiro_crew.slack.events_queue import _dispatch_queued  # noqa: E402
from kiro_crew.slack.events_queue import _drain_slack_queue  # noqa: E402
from kiro_crew.slack.events_queue import _entry_owner  # noqa: E402
from kiro_crew.slack.events_queue import _key_busy  # noqa: E402
from kiro_crew.slack.events_queue import _queue_tags  # noqa: E402
from kiro_crew.slack.events_queue import _resolve_approval_mode  # noqa: E402
from kiro_crew.slack.events_queue import _settle_stop_hold  # noqa: E402
from kiro_crew.slack.events_queue import _StopHold  # noqa: E402
from kiro_crew.slack.events_queue import _stops_in_flight  # noqa: E402
from kiro_crew.slack.events_queue import _track_router_task  # noqa: E402
from kiro_crew.slack.events_slash import _handle_restart  # noqa: E402
from kiro_crew.slack.events_slash import _handle_slash  # noqa: E402
from kiro_crew.slack.events_slash import _safe_log  # noqa: E402
from kiro_crew.slack.events_voice import _transcribe_with_reaction  # noqa: E402
from kiro_crew.slack.events_voice import _voice_memo_context  # noqa: E402

#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.slack.events_block_text",
    "kiro_crew.slack.events_queue",
    "kiro_crew.slack.events_slash",
    "kiro_crew.slack.events_home_tab",
    "kiro_crew.slack.events_voice",
    "kiro_crew.slack.events_inbound",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name the module and its owners bind.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
# isort: off
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.slack.events_block_text import _BoundedBlockText  # noqa: F401
    from kiro_crew.slack.events_block_text import _MAX_RECOVERED_TEXT_CHARS  # noqa: F401
    from kiro_crew.slack.events_block_text import _extract_blocks_text  # noqa: F401
    from kiro_crew.slack.events_block_text import _normalize_message_blocks  # noqa: F401
    from kiro_crew.slack.events_block_text import _render_rich_text_element  # noqa: F401
    from kiro_crew.slack.events_block_text import _table_row_fragments  # noqa: F401
    from kiro_crew.slack.events_block_text import _until_full  # noqa: F401
    from kiro_crew.slack.events_queue import _bg_tasks  # noqa: F401
    from kiro_crew.slack.events_queue import _on_tracked_done  # noqa: F401
    from kiro_crew.slack.events_queue import _router_task_threads  # noqa: F401
    from kiro_crew.slack.events_queue import _spawn_tracked  # noqa: F401
    from kiro_crew.slack.events_queue import router_task_thread  # noqa: F401
    from kiro_crew.slack.events_queue import slack_stop_in_flight  # noqa: F401
    from kiro_crew.slack.events_slash import SLASH_REGISTRY  # noqa: F401
    from kiro_crew.slack.events_slash import SlashHandler  # noqa: F401
    from kiro_crew.slack.events_slash import _MANIFEST_COMMAND_RE  # noqa: F401
    from kiro_crew.slack.events_slash import _build_help_text  # noqa: F401
    from kiro_crew.slack.events_slash import _get_agent_names  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_agent  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_allowlist_cmd  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_channel_cmd  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_config  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_dashboard  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_sessions  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_status  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_voice  # noqa: F401
    from kiro_crew.slack.events_slash import _handle_yolo  # noqa: F401
    from kiro_crew.slack.events_slash import _selector_agent_names  # noqa: F401
    from kiro_crew.slack.events_slash import register_slash_command  # noqa: F401
    from kiro_crew.slack.events_home_tab import _HOME_TAB_COLLECT_CONCURRENCY  # noqa: F401
    from kiro_crew.slack.events_home_tab import _get_skills_loader  # noqa: F401
    from kiro_crew.slack.events_home_tab import _home_tab_collect_gate  # noqa: F401
    from kiro_crew.slack.events_home_tab import _home_tab_collect_sem  # noqa: F401
    from kiro_crew.slack.events_home_tab import _skills_loader  # noqa: F401
    from kiro_crew.slack.events_voice import _transcribe_files  # noqa: F401
    from kiro_crew.slack.events_inbound import TrustedBotTurnLedger  # noqa: F401
    from kiro_crew.slack.events_inbound import _LEADING_MENTIONS_RE  # noqa: F401
    from kiro_crew.slack.events_inbound import _MAX_SEEN  # noqa: F401
    from kiro_crew.slack.events_inbound import _USER_MENTION_RE  # noqa: F401
    from kiro_crew.slack.events_inbound import _addressed_to_someone_else  # noqa: F401
# isort: on
