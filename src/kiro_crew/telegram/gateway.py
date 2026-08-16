"""Telegram channel startup -- wired into the gateway boot.

``maybe_start_telegram`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`TelegramDispatcher` +
:class:`TelegramTransport` + the low-level :class:`TelegramClient`, wires the
client's inbound long-poll into ``transport.receive`` (authorize + normalize)
and its inline-button presses into ``dispatcher.on_callback``, then starts
polling via ``transport.connect()``. Failures are logged and swallowed so a
Telegram problem never takes down the gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.telegram.client import TelegramAuthError, TelegramClient
from kiro_crew.telegram.commands import bot_command_payload
from kiro_crew.telegram.transport import TelegramTransport
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Slack path, neutral).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default unless a decider/hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_telegram(orch: "GatewayOrchestrator") -> "TelegramClient | None":
    """Start the Telegram channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references. Token / enabled / allowed_user_ids are read once in the
    orchestrator's constructor and consumed off ``orch`` here.
    """
    if not getattr(orch, "_telegram_enabled", False):
        return None
    bot_token = getattr(orch, "_telegram_bot_token", "")
    if not bot_token:
        return None

    client: "TelegramClient | None" = None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_ids: set[int] = set(getattr(orch, "_telegram_allowed_user_ids", []) or [])
        if not allowed_ids:
            logger.warning(
                "Telegram: allowed_user_ids is empty — the bot is globally "
                "reachable but will REJECT all messages (fail closed). Add your "
                "numeric Telegram user_id to telegram.allowed_user_ids to enable."
            )

        dispatcher = TelegramDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            allowed_user_ids=allowed_ids,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = TelegramClient(token=bot_token, on_callback=dispatcher.on_callback)
        transport = TelegramTransport(
            client,
            allowed_user_ids=allowed_ids,
            allow_forum=bool(getattr(orch, "_telegram_allow_forum", False)),
            allowed_forum_chat_ids=list(
                getattr(orch, "_telegram_allowed_forum_chat_ids", []) or []
            ),
            dispatch=dispatcher.handle_message,
        )
        # Inbound: client long-poll -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        # Prove the token with an authenticated call BEFORE reporting the
        # channel as connected — transport.connect() only schedules the
        # polling task, so without this a bad/revoked token would show
        # "Connected" in settings while the bot receives nothing. A rejected
        # token (TelegramAuthError) is fatal and lands in the except below;
        # a transport error means we're merely offline, which is NOT a bad
        # token: start polling anyway (it backs off and retries) and report
        # not-connected until the first successful poll flips the status.
        token_ok = False
        startup_error = ""
        try:
            me = await client.get_me()
            token_ok = True
            # Gates @BotUsername suffix stripping in command parsing (see
            # telegram/commands.py._strip_bot_mention): without this, a
            # command addressed to a DIFFERENT bot in the same group would be
            # mistaken for ours.
            dispatcher.bot_username = me.get("username", "") or ""
        except TelegramAuthError:
            raise
        except Exception as exc:
            startup_error = f"Telegram unreachable at startup ({type(exc).__name__})"
            logger.warning("%s — starting polling anyway (will retry).", startup_error)

        if token_ok:
            # Publish the "/" autocomplete menu so the commands are discoverable
            # in the Telegram client instead of only via /help. Best-effort and
            # gated on a proven token: a failure here costs autocomplete, never
            # the channel, and an unreachable Telegram would fail anyway.
            try:
                await client.set_my_commands(bot_command_payload())
            except Exception:
                logger.warning("Telegram: setMyCommands failed", exc_info=True)

        await transport.connect()  # starts the long-polling loop
        assert client is not None  # constructed above; narrows the Optional for mypy
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
            orch.dashboard_state.telegram_connected = token_ok
            orch.dashboard_state.telegram_connect_error = "" if token_ok else startup_error

            # Keep the badge truthful after startup: the polling loop reports
            # persistent getUpdates failures (e.g. token revoked mid-session)
            # and recovery through this callback, deduped on state change.
            state = orch.dashboard_state

            def _on_status(healthy: bool, reason: str) -> None:
                state.telegram_connected = healthy
                state.telegram_connect_error = "" if healthy else reason[:120]

            client._last_status = token_ok  # transitions relative to boot state
            client.on_status = _on_status
        logger.info("Telegram channel started (transport path, long-polling).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            # TelegramAuthError carries a fixed, token-free message; for
            # anything else surface only the exception class (aiohttp error
            # strings can embed the request URL, which contains the token).
            reason = str(exc) if isinstance(exc, TelegramAuthError) else type(exc).__name__
            orch.dashboard_state.telegram_connect_error = reason[:120]
        # Don't leak the aiohttp session (opened by getMe) when startup
        # fails — the client is not returned, so nothing else would close it.
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("Telegram client close after failed start", exc_info=True)
        logger.exception("Failed to start Telegram channel; continuing without it.")
        return None
