"""Discord channel startup -- wired into the gateway boot.

``maybe_start_discord`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`DiscordDispatcher` +
:class:`DiscordTransport` + the low-level :class:`DiscordClient`, wires the
client's inbound Gateway events into ``transport.receive`` (authorize +
normalize) and its button presses into ``dispatcher.on_interaction``, then
starts the Gateway WebSocket via ``transport.connect()``. Failures are logged
and swallowed so a Discord problem never takes down the gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction
+ tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING

from kiro_crew.discord.client import DiscordClient
from kiro_crew.discord.commands import application_command_payload
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.discord.transport_dispatch import _CHANNEL, DiscordDispatcher
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.messaging.queue_drain import unregister_drain
from kiro_crew.messaging.spawn_approval_delivery import (
    SpawnApprovalDeliveryHook,
    register_channel_delivery,
    unregister_channel_delivery,
)
from kiro_crew.messaging.transport import InboundMessage

if TYPE_CHECKING:
    from collections.abc import Callable

    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _client_close_hook(
    dispatcher: DiscordDispatcher, delivery_hook: SpawnApprovalDeliveryHook
) -> "Callable[[], None]":
    """What the client runs when it closes: retire this dispatcher's two registrations.

    Its spawn-approval delivery, so the host spawn gate stops routing to it. And its
    queue drain, so a drain waiting on a held lease
    (``messaging.queue_drain.drain_when_free``), which looks the drain up when the
    lease frees, finds none instead of answering on a channel the operator disabled.

    Both are compare-and-drop against this dispatcher's own objects, so a restart
    whose replacement already registered keeps its registrations.
    """
    drain_hook = dispatcher._drain_queue

    def _on_close() -> None:
        unregister_channel_delivery("discord", delivery_hook)
        unregister_drain(_CHANNEL, drain_hook)

    return _on_close


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Telegram path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default unless a decider/hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


#: How long the background registration waits for the Gateway handshake. Longer
#: than the caller's own readiness wait, because this task is off the boot path
#: and its only cost for waiting is a later slash menu.
_REGISTER_READY_TIMEOUT_SECS = 30.0


def _publish_slash_commands(client: "DiscordClient") -> None:
    """Publish the ``/`` command menu in the background, best-effort.

    Fired as a task rather than awaited: registration is a REST round-trip whose
    only product is discoverability, and the gateway boot path must not grow an
    awaited network step (every user pays it on every launch, and a slow boot is
    what the loop-stall watchdog turns into a crash loop). The ``!`` text commands
    are the floor and work regardless, so a failure here costs the slash menu and
    nothing else.

    It needs READY to have populated ``application_id``; when the handshake did
    not land, ``register_application_commands`` reports that and returns False.
    """

    async def _register() -> None:
        try:
            # Wait for READY here rather than relying on the caller having done
            # it: the caller's wait sits inside its dashboard-state branch, so a
            # `--no-dashboard` gateway reached this with no handshake yet and an
            # empty application id, and the menu was skipped on exactly the
            # install that has no other way to discover the commands. Waiting in
            # the task keeps the boot path free of the await either way.
            if not await client.wait_ready(timeout=_REGISTER_READY_TIMEOUT_SECS):
                logger.warning(
                    "Discord: gateway not READY within %.0fs, so the slash-command "
                    "menu was not published; the ! text commands are unaffected",
                    _REGISTER_READY_TIMEOUT_SECS,
                )
                return
            await client.register_application_commands(application_command_payload())
        except Exception:
            logger.warning("Discord: publishing the slash-command menu failed", exc_info=True)

    task = asyncio.create_task(_register())
    # Hold a reference so the task is not garbage-collected mid-flight, and drop
    # it on completion.
    _PENDING_REGISTRATIONS.add(task)
    task.add_done_callback(_PENDING_REGISTRATIONS.discard)


#: In-flight command-registration tasks, held only to keep them from being
#: collected before they finish.
_PENDING_REGISTRATIONS: set[asyncio.Task[None]] = set()


def _reopen_owner_dm(client: "DiscordClient", allowed_ids: set[str]) -> None:
    """Re-open the sole owner's DM in the background, so its pairing is known again.

    The ``DM channel -> user`` pairing lives in this process only, and the owner-DM
    predicate refuses a session mirrored to a DM it cannot place
    (``MIRROR_PEER_NOT_ON_RECORD``). Without this, a conductor whose thread was
    linked to the owner's DM before a restart could not reach any of its workers
    until the owner wrote into the DM. ``create_dm_channel`` is the call the
    dashboard's connect row makes to open that DM, and it records the pairing from
    Discord's own answer. Fired as a task for the reason the slash menu is.
    """
    if len(allowed_ids) != 1:
        return
    (owner,) = allowed_ids

    async def _reopen() -> None:
        try:
            if await client.wait_ready(timeout=_REGISTER_READY_TIMEOUT_SECS):
                await client.create_dm_channel(owner)
        except Exception:
            logger.warning("Discord: re-opening the owner's DM failed", exc_info=True)

    task = asyncio.create_task(_reopen())
    _PENDING_REGISTRATIONS.add(task)
    task.add_done_callback(_PENDING_REGISTRATIONS.discard)


#: How long a reconnect waits for the new connection to reach READY.
_RECONNECT_READY_TIMEOUT_SECS = 15.0

#: Shown when Discord refuses the server-thread intents on a reconnect.
INTENTS_REFUSED = (
    "Discord refused the server-thread intents (gateway close 4014). Turn on "
    "Message Content Intent on the Bot page of the Developer Portal, then save "
    "again. DMs still work."
)

#: One lock per live client, so two reconnects never swap the same connection at once.
_RECONNECT_LOCKS: "weakref.WeakKeyDictionary[DiscordClient, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _note_reconnect_error(orch: "GatewayOrchestrator", error: str) -> None:
    """Record what the saved settings ask for that the live client could not take.

    Every attempt sets it, empty when nothing is left unapplied, so it always
    describes the latest attempt rather than an earlier save's.
    """
    state = orch.dashboard_state
    if state is not None:
        state.discord_reconnect_error = error[:300]


#: How long a settings save waits for its reconnect. Past it the save answers
#: without the outcome; the status badge catches up.
_SAVE_WAIT_SECS = 20.0


async def reconcile_after_save(orch: "GatewayOrchestrator") -> None:
    """Bring the live client in line with what a settings save stored.

    Waits for the reconnects and restarts the save's write scheduled, then
    reconnects once more. The second step applies a save that changed nothing
    on disk: after Discord refused the thread intents (4014) and the operator
    turned on Message Content Intent, "save again" writes the same settings,
    so the config watcher sees no change and schedules nothing.
    """
    settled = getattr(orch, "channel_restarts_settled", None)
    if settled is not None:
        await settled()
    try:
        await reconnect_discord(orch)
    except Exception:
        logger.warning("Discord: reconciling after a settings save failed", exc_info=True)


async def reconcile_for_save(state: object) -> str:
    """Run the save's reconcile (bounded); return what the live client could not take.

    Empty when nothing failed, when Discord never started (no hook), or when the
    wait ran out.
    """
    reconcile = getattr(state, "discord_reconcile", None)
    if reconcile is None:
        return ""
    try:
        await asyncio.wait_for(asyncio.shield(reconcile()), _SAVE_WAIT_SECS)
    except asyncio.TimeoutError:
        return ""
    return str(getattr(state, "discord_reconnect_error", "") or "")


def _admits_live_threads(client: DiscordClient) -> bool:
    """Whether *client*'s transport still admits a server thread the config does not list.

    A thread the bot opened under an allowed channel is admitted in memory
    (``DiscordTransport.reconfigure`` keeps it when its parent is removed), so
    dropping the thread intents would cut off a conversation still allowed. The
    transport is the owner of the bound inbound handler (``transport.receive``).
    """
    transport = getattr(client._on_message, "__self__", None)
    return bool(getattr(transport, "_allowed_threads", None))


async def _connect_permitted() -> bool:
    """The connect-time ``channels`` governance gate a channel start passes, off the loop."""
    # circular import: the host gateway imports the channel roster, which imports this module
    from kiro_crew.slack import gateway as host

    loop = asyncio.get_running_loop()
    return bool(
        await loop.run_in_executor(
            host.maintenance_executor(), host._channel_transport_permitted, "discord"
        )
    )


async def reconnect_discord(orch: "GatewayOrchestrator") -> bool:
    """Apply the saved bot token and the intents the allow-lists need, in place.

    The reusable reconnect: it reads the current config and credentials and
    makes the live client match them, re-IDENTIFYing only when the token or
    the intents differ (a server thread or channel on the allow-list needs
    GUILD_MESSAGES + MESSAGE_CONTENT; DM-only needs neither). The dispatcher,
    transport and any running turn are kept, as Slack's reconnect keeps them.

    Returns True when the change is handled here: applied, nothing to apply,
    or a token Discord rejected (the old connection keeps running and the
    reason is recorded). Returns False when the caller must restart the
    channel instead (``restart_channel``): no live client, Discord turned off
    or its token cleared, Discord unreachable for the token check, or a token
    for a different bot, whose threads and sessions belong to the old one, or a
    ``channels`` policy that does not permit the connection (a new
    connection passes the same connect-time gate a channel start does).
    No token value is logged or recorded.
    """
    handles: dict[str, object] = getattr(orch, "_channel_handles", None) or {}
    client = handles.get("discord")
    if not isinstance(client, DiscordClient):
        _note_reconnect_error(orch, "")
        return False
    lock = _RECONNECT_LOCKS.setdefault(client, asyncio.Lock())
    async with lock:
        applied, error = await _reconnect_locked(orch, handles, client)
        # Written once, when the attempt has finished, so a save reading the
        # outcome never sees a value an attempt still in flight cleared.
        if error is not None:
            _note_reconnect_error(orch, error)
        return applied


async def _reconnect_locked(
    orch: "GatewayOrchestrator", handles: dict[str, object], client: DiscordClient
) -> tuple[bool, str | None]:
    """The body of :func:`reconnect_discord`; the caller holds the client's lock.

    Returns ``(applied, error)``: *error* is what is left unapplied ("" for
    nothing), or None when another start replaced the client and its own
    attempt reports.
    """
    from kiro_crew.config import live
    from kiro_crew.config.loader import CRED_DISCORD_BOT_TOKEN, KiroCrewConfig

    # Read under the lock, so an attempt that queued behind another applies the
    # latest saved settings, not a snapshot read before it waited.
    cfg = live.snapshot() or await asyncio.to_thread(KiroCrewConfig.load)
    if cfg is None:
        return False, ""
    creds = await asyncio.to_thread(cfg.load_credentials, propagate=False)
    dc = cfg.discord
    token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or dc.bot_token
    if not (dc.enabled and token):
        return False, ""
    guild = bool(dc.allowed_thread_ids or dc.allowed_channel_ids) or _admits_live_threads(client)
    same_token = client.uses_token(token)
    # A client a fatal close stopped (a 4014 at boot, say) is reconnected even
    # when its settings match: that is how a save after the fix revives it.
    if same_token and client.guild_intents == guild and not client.fatal_error:
        return True, ""
    new_token: str | None = None
    if not same_token:
        try:
            bot_id, rejected = await client.verify_token(token)
        except Exception as exc:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs the exception class only, never the token value
            logger.warning(
                "Discord: could not check the new bot token (%s); restarting the channel",
                type(exc).__name__,
            )
            return False, ""
        if rejected:
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs Discord's short refusal message only, never the token value
            logger.warning(
                "Discord rejected the new bot token (%s); the current connection keeps running",
                rejected,
            )
            return True, (
                f"Discord rejected the new bot token ({rejected}); the previous "
                "connection is still running."
            )
        if not client.bot_user_id or bot_id != client.bot_user_id:
            logger.info("Discord: the new token is for another bot; restarting the channel")
            return False, ""
        new_token = token
    if not await _connect_permitted():
        logger.info("Discord: the channels policy does not permit a reconnect; restarting")
        return False, ""
    # Under the channel restart lock, so a restart or a disable cannot close
    # this client between the check and the swap.
    async with orch._channel_restart_lock:
        if handles.get("discord") is not client:
            # A restart replaced or closed this client while the token was checked.
            return True, None
        # READY never landed (a 4014 at boot, say): the boot steps that wait for
        # it gave up, so they are scheduled again to wait for this connection.
        never_ready = not client.application_id
        await client.reconnect(token=new_token, enable_guild_threads=guild)
        orch._discord_bot_token = token
    if never_ready:
        _publish_slash_commands(client)
        _reopen_owner_dm(client, {str(u) for u in dc.allowed_user_ids})
    logger.info(
        "Discord reconnected in place (%s%s)",
        "new token, " if new_token else "",
        "server threads" if guild else "DMs only",
    )
    if await client.wait_connected(_RECONNECT_READY_TIMEOUT_SECS):
        return True, ""
    if guild and "4014" in client.fatal_error:
        logger.error(INTENTS_REFUSED)
        async with orch._channel_restart_lock:
            if handles.get("discord") is not client:
                return True, None
            await client.reconnect(enable_guild_threads=False)
        await client.wait_connected(_RECONNECT_READY_TIMEOUT_SECS)
        return True, INTENTS_REFUSED
    if client.fatal_error:
        return True, f"Discord {client.fatal_error}"
    return True, ""


async def maybe_start_discord(orch: "GatewayOrchestrator") -> "DiscordClient | None":
    """Start the Discord channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references. Token / enabled / allow-lists are hoisted onto ``orch`` and
    consumed here; a later token or intents change is applied to the running
    client by :func:`reconnect_discord`.
    """
    if not getattr(orch, "_discord_enabled", False):
        return None
    bot_token = getattr(orch, "_discord_bot_token", "")
    if not bot_token:
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_ids: set[str] = set(getattr(orch, "_discord_allowed_user_ids", []) or [])
        allowed_threads: set[str] = set(getattr(orch, "_discord_allowed_thread_ids", []) or [])
        allowed_channels: set[str] = set(getattr(orch, "_discord_allowed_channel_ids", []) or [])
        auto_thread = bool(getattr(orch, "_discord_auto_thread", True))
        if not allowed_ids:
            logger.warning(
                "Discord: allowed_user_ids is empty — the bot is reachable by "
                "anyone sharing a server with it but will REJECT all messages "
                "(fail closed). Add your Discord user ID (snowflake) to "
                "discord.allowed_user_ids to enable."
            )

        dispatcher = DiscordDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            allowed_user_ids=allowed_ids,
            allowed_thread_ids=allowed_threads,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = DiscordClient(
            token=bot_token,
            on_interaction=dispatcher.on_interaction,
            enable_guild_threads=bool(allowed_threads or allowed_channels),
        )

        async def _dispatch(message: InboundMessage) -> None:
            await dispatcher.handle_message(message)

        transport = DiscordTransport(
            client,
            allowed_user_ids=allowed_ids,
            allowed_thread_ids=allowed_threads,
            allowed_channel_ids=allowed_channels,
            auto_thread=auto_thread,
            on_thread_created=dispatcher.register_allowed_thread,
            dispatch=_dispatch,
        )
        # Inbound: Gateway WS -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client
        # Handed to the dispatcher so its config applier can push a reloaded
        # allow-list at the live transport instead of waiting for a restart.
        dispatcher.transport = transport

        # Channel-side spawn-approval delivery. Register this dispatcher's
        # in-channel Approve/Deny prompt as the "discord" surface the host spawn
        # gate consults before its Slack-DM/dashboard fallback, and retire it when
        # the client shuts down so the gate stops routing to a dispatcher that is
        # going away. Idempotent: a restart replaces this channel's own hook.
        # The hook is bound ONCE and the same object is handed to both calls, so the
        # close is a compare-and-drop: a restart whose replacement hook already took
        # the slot is not unregistered by this (older) client's close.
        delivery_hook = dispatcher.deliver_spawn_approval
        register_channel_delivery("discord", delivery_hook)
        client.on_close = _client_close_hook(dispatcher, delivery_hook)

        await transport.connect()  # starts the Gateway WebSocket loop
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
            state = orch.dashboard_state
            # Let a session-resume bind/release refresh the dashboard at once,
            # so the "driven from Discord" chip is never missing while the
            # two-way delivery is already active.
            dispatcher._session_resume.dashboard_state = state

            # Keep the status badge truthful across the channel's lifetime:
            # READY/RESUMED mark connected; a non-recoverable close (bad token
            # or intents) flips it back off with the reason.
            def _on_state(connected: bool, error: str) -> None:
                state.discord_connected = connected
                state.discord_connect_error = error[:120]

            client.on_state_change = _on_state
            # The settings save runs this so it can answer with what the live
            # client could not take from the settings it stored.
            state.discord_reconcile = lambda: reconcile_after_save(orch)
            # "Connected" only once the Gateway handshake actually reaches
            # READY — connect() merely schedules the loop, and reporting
            # success before the handshake would show a green badge on a bad
            # token. On timeout the badge stays off with an actionable reason.
            if await client.wait_ready(timeout=15.0):
                state.discord_connected = True
                state.discord_connect_error = ""
            else:
                state.discord_connected = False
                state.discord_connect_error = (
                    client.fatal_error or "gateway not READY within 15s (check bot token/network)"
                )
        _publish_slash_commands(client)
        _reopen_owner_dm(client, allowed_ids)
        logger.info("Discord channel started (transport path, Gateway WebSocket).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.discord_connect_error = type(exc).__name__[:120]
        logger.exception("Failed to start Discord channel; continuing without it.")
        return None
