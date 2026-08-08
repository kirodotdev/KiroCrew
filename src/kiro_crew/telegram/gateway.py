"""Telegram channel startup -- wired into the gateway boot.

``maybe_start_telegram`` is the single guarded entry point. When the channel is
enabled + credentialed it builds one :class:`TelegramDispatcher` +
:class:`TelegramTransport` + :class:`TelegramClient` **per configured
account**, enabling a single gateway process to serve multiple Telegram bots
simultaneously. Failures per account are logged and swallowed so one bad token
never takes down the others or the gateway.

Multi-account support: when ``telegram.accounts`` is populated in config, each
named account gets its own polling loop, dispatcher, and session namespace.
When absent, the legacy single-token config is auto-wrapped as a ``"default"``
account for full backward compatibility.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.config.loader import TelegramAccountConfig
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.telegram.client import TelegramAuthError, TelegramClient
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


def _resolve_agent_for_account(orch: "GatewayOrchestrator", account_id: str) -> str | None:
    """Find the agent bound to a Telegram account via the agents config map.

    Scans ``cfg.agents`` for an entry with ``telegram_account == account_id``.
    Returns the agent name (key) if found, else None (routes to default agent).
    """
    agents_map = getattr(orch._cfg, "agents", {})
    if not isinstance(agents_map, dict):
        return None
    for agent_name, agent_cfg in agents_map.items():
        tg_account = getattr(agent_cfg, "telegram_account", "") or ""
        if tg_account == account_id:
            return agent_name
    return None


async def _start_single_account(
    orch: "GatewayOrchestrator",
    account_id: str,
    account_cfg: "TelegramAccountConfig",
) -> "TelegramClient | None":
    """Start a single Telegram bot account. Returns the client or None on failure."""
    bot_token = account_cfg.bot_token
    if not bot_token:
        return None

    client: "TelegramClient | None" = None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_ids: set[int] = set(account_cfg.allowed_user_ids or [])
        if not allowed_ids:
            logger.warning(
                "Telegram account %r: allowed_user_ids is empty — the bot is "
                "globally reachable but will REJECT all messages (fail closed). "
                "Add numeric Telegram user_id(s) to enable.",
                account_id,
            )

        # Determine the channel name for session key isolation.
        # The "default" account keeps "telegram" for backward compatibility;
        # named accounts use "telegram.{account_id}" to isolate sessions.
        # (dot separator, not colon — colon is the session-key field delimiter)
        # TODO: register "telegram.{id}" in CHANNEL_SESSION_NAMESPACES so
        # dashboard slots, telemetry, and autonudge recognize named accounts.
        # Until then, named-account sessions classify as "other" in telemetry
        # and don't get dashboard chat slots — acceptable for the initial
        # multi-account release (DM-only, no /link support for named accounts).
        channel_name = "telegram" if account_id == "default" else f"telegram.{account_id}"

        # Resolve which agent this account is bound to (if any).
        bound_agent = _resolve_agent_for_account(orch, account_id)

        # Forum Topics are only supported for the "default" account because the
        # callback path (on_callback in the dispatcher) reads forum policy from
        # the global cfg.telegram, not per-account config. Until callbacks are
        # account-aware, named accounts run DM-only (fail closed on groups).
        effective_allow_forum = bool(account_cfg.allow_forum) if account_id == "default" else False
        effective_forum_chat_ids = (
            list(account_cfg.allowed_forum_chat_ids or []) if account_id == "default" else []
        )

        dispatcher = TelegramDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            allowed_user_ids=allowed_ids,
            agent=bound_agent,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
            channel_name=channel_name,
        )
        client = TelegramClient(token=bot_token, on_callback=dispatcher.on_callback)
        transport = TelegramTransport(
            client,
            allowed_user_ids=allowed_ids,
            allow_forum=effective_allow_forum,
            allowed_forum_chat_ids=effective_forum_chat_ids,
            dispatch=dispatcher.handle_message,
        )
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        token_ok = False
        startup_error = ""
        try:
            await client.get_me()
            token_ok = True
        except TelegramAuthError:
            raise
        except Exception as exc:
            startup_error = (
                f"Telegram account {account_id!r} unreachable at startup "
                f"({type(exc).__name__})"
            )
            logger.warning("%s — starting polling anyway (will retry).", startup_error)

        await transport.connect()
        assert client is not None

        if orch.dashboard_state is not None:
            # Only register the default account's transport for dashboard
            # cross-surface features (/link). Named accounts use separate
            # session namespaces and don't participate in dashboard mirroring
            # until transport keys are account-aware (future PR).
            if account_id == "default":
                orch.dashboard_state.register_channel_transport(transport)
                orch.dashboard_state.telegram_connected = token_ok
                orch.dashboard_state.telegram_connect_error = (
                    "" if token_ok else startup_error
                )

                state = orch.dashboard_state

                def _on_status(healthy: bool, reason: str) -> None:
                    state.telegram_connected = healthy
                    state.telegram_connect_error = "" if healthy else reason[:120]

                client._last_status = token_ok
                client.on_status = _on_status

        logger.info(
            "Telegram account %r started (transport path, long-polling).", account_id
        )
        return client
    except Exception as exc:
        if orch.dashboard_state is not None and account_id == "default":
            reason = (
                str(exc) if isinstance(exc, TelegramAuthError) else type(exc).__name__
            )
            orch.dashboard_state.telegram_connect_error = reason[:120]
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug(
                    "Telegram client close after failed start (account %r)",
                    account_id,
                    exc_info=True,
                )
        logger.exception(
            "Failed to start Telegram account %r; continuing without it.", account_id
        )
        return None


async def maybe_start_telegram(
    orch: "GatewayOrchestrator",
) -> "TelegramClient | _MultiClientHandle | None":
    """Start the Telegram channel if enabled + credentialed; else no-op.

    Supports multiple bot accounts via ``telegram.accounts`` in config.
    Returns a single client (backward compat for single-account), a multi-client
    handle (multi-account), or None. The returned handle exposes ``.close()``
    for the registry shutdown loop.
    """
    if not getattr(orch, "_telegram_enabled", False):
        return None

    accounts = orch._cfg.telegram.resolved_accounts()
    # The env-resolved token (TELEGRAM_BOT_TOKEN credential) takes precedence
    # over cfg.telegram.bot_token. Override ONLY when the account map was
    # auto-synthesized from the legacy single-token config (no explicit
    # telegram.accounts section). When accounts are explicitly configured,
    # each account's bot_token is authoritative — the env var does not clobber.
    env_token = getattr(orch, "_telegram_bot_token", "")
    has_explicit_accounts = bool(orch._cfg.telegram.accounts)
    if env_token and not has_explicit_accounts and "default" in accounts:
        accounts["default"] = TelegramAccountConfig(
            bot_token=env_token,
            allowed_user_ids=accounts["default"].allowed_user_ids,
            allow_forum=accounts["default"].allow_forum,
            allowed_forum_chat_ids=accounts["default"].allowed_forum_chat_ids,
            soft_threshold_pct=accounts["default"].soft_threshold_pct,
        )
    elif env_token and not has_explicit_accounts and not accounts:
        # No accounts configured and no cfg bot_token, but env token exists.
        accounts = {
            "default": TelegramAccountConfig(
                bot_token=env_token,
                allowed_user_ids=list(getattr(orch, "_telegram_allowed_user_ids", []) or []),
                allow_forum=bool(getattr(orch, "_telegram_allow_forum", False)),
                allowed_forum_chat_ids=list(
                    getattr(orch, "_telegram_allowed_forum_chat_ids", []) or []
                ),
            )
        }

    if not accounts:
        return None

    clients: list[TelegramClient] = []
    for account_id, account_cfg in accounts.items():
        client = await _start_single_account(orch, account_id, account_cfg)
        if client is not None:
            clients.append(client)

    if not clients:
        return None
    # Single account: return the client directly for backward compat with
    # existing shutdown code that expects a single closeable handle.
    if len(clients) == 1:
        return clients[0]
    return _MultiClientHandle(clients)


class _MultiClientHandle:
    """Thin wrapper exposing a single ``.close()`` for multiple TelegramClients.

    The channel registry calls ``handle.close()`` on shutdown; this fans it out
    to every underlying client.
    """

    __slots__ = ("_clients",)

    def __init__(self, clients: list["TelegramClient"]) -> None:
        self._clients = clients

    async def close(self) -> None:
        """Close all clients; errors in one do not prevent closing the rest."""
        for client in self._clients:
            try:
                await client.close()
            except Exception:
                logger.debug("Multi-client close error", exc_info=True)
