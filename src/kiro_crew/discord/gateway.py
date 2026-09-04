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
from typing import TYPE_CHECKING

from kiro_crew.discord.client import DiscordClient
from kiro_crew.discord.commands import application_command_payload
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.messaging.transport import InboundMessage

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


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


async def maybe_start_discord(orch: "GatewayOrchestrator") -> "DiscordClient | None":
    """Start the Discord channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references. Token / enabled / allow-lists are read once in the
    orchestrator's constructor and consumed off ``orch`` here.
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
        logger.info("Discord channel started (transport path, Gateway WebSocket).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.discord_connect_error = type(exc).__name__[:120]
        logger.exception("Failed to start Discord channel; continuing without it.")
        return None
