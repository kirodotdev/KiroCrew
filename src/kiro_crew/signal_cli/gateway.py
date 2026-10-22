"""Signal channel startup -- wired into the gateway boot.

``maybe_start_signal`` is the single guarded entry point. When the channel is
enabled it builds the :class:`SignalDispatcher` + :class:`SignalTransport` + the
low-level :class:`SignalClient`, wires inbound ``receive`` notifications into
``transport.receive`` (own-traffic suppression + group fail-close + authorize ->
dispatcher), connects, and registers the transport with the dashboard state so
the shared proactive-send ladder (``send_message`` MCP tool, notifications,
cron results, the mirror) can deliver to Signal. Failures are logged and
swallowed so a Signal problem never takes down the gateway.

A failed start is reported AND retried: signal-cli's last stderr line (e.g.
"User +44... is not registered.") lands in
``dashboard_state.signal_connect_error``, and the client keeps trying on its
capped exponential backoff, because boot-time failures are often transient (no
network yet, a held account lock, a slow JVM). A child that exits after a good
start goes through the same loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.signal_cli.client import SignalClient, account_identifier, redact_identifier
from kiro_crew.signal_cli.rpc import RpcTransportError
from kiro_crew.signal_cli.transport import (
    SignalTransport,
    allowed_numbers_from_config,
    warn_unusable_entries,
)
from kiro_crew.signal_cli.transport_dispatch import SignalDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

#: How long boot waits for the probe's ready signal after ``connect``.
READY_TIMEOUT_S = 15.0


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """YOLO -> auto; else the CLI override or ``agent.approval_mode`` (mirrors WhatsApp).

    ``interactive`` is what makes the dispatcher install the typed-reply
    approval decider on each operator turn; ``auto`` installs none.
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


def _report_error(orch: "GatewayOrchestrator", message: str) -> None:
    state = getattr(orch, "dashboard_state", None)
    if state is not None:
        state.signal_connected = False
        state.signal_connect_error = message[:120]


def _state_mirror(state: Any) -> Callable[[bool, str], None]:
    """Keep the dashboard badge truthful across the channel's lifetime."""

    def _on_state(connected: bool, error: str) -> None:
        state.signal_connected = connected
        state.signal_connect_error = error[:120]

    return _on_state


def build_signal_channel(
    orch: "GatewayOrchestrator", section: Any
) -> tuple[SignalClient, SignalTransport, SignalDispatcher]:
    """Construct the client/transport/dispatcher triple, wired, not yet connected.

    Split out of :func:`maybe_start_signal` so a test (or a future in-process
    restart) can build the real object graph without spawning anything.
    """
    assert orch.sessions is not None and orch.ctx_builder is not None
    cfg = orch._cfg
    allowed = allowed_numbers_from_config(getattr(section, "allowed_numbers", None)) or []
    dispatcher = SignalDispatcher(
        sessions=orch.sessions,
        ctx_builder=orch.ctx_builder,
        cfg=cfg,
        agent=None,
        conv_log=getattr(orch, "conv_log", None),
        approval_mode=_resolve_approval_mode(orch),
    )
    client = SignalClient(account_identifier(getattr(section, "account", "")))
    transport = SignalTransport(client, allowed_numbers=allowed, dispatch=dispatcher.handle_message)
    dispatcher.transport = transport
    dispatcher.client = client
    client.set_message_handler(transport.receive)
    return client, transport, dispatcher


async def maybe_start_signal(orch: "GatewayOrchestrator") -> "SignalClient | None":
    """Start the Signal channel if enabled. Returns the running client or None.

    The gateway ``close()``s the returned client on shutdown; the transport and
    dispatcher stay alive through the client's handler references and the
    dashboard state's transport registry.

    A signal-cli that fails its first start is NOT given up on: the transport is
    registered anyway (so a proactive send reaches Signal the moment it is up)
    and the client keeps retrying on its restart backoff, updating
    ``signal_connect_error`` each round. Only a missing ``signal.account`` --
    which nothing but a config change can fix, and a config change restarts the
    channel -- returns None.
    """
    if not getattr(orch, "_signal_enabled", False):
        return None
    client: SignalClient | None = None
    try:
        section = getattr(orch._cfg, "signal", None)
        raw_account = getattr(section, "account", "")
        # Normalized as the client reads it (formatting dropped, 00 read as +), so
        # the boot gate, the child's argv and doctor all agree on one account.
        account = account_identifier(raw_account)
        if not account:
            message = "signal.account is not set; channel not started"
            logger.warning("Signal: %s.", message)
            _report_error(orch, message)
            return None
        warn_unusable_entries(raw_account, field="signal.account")
        raw_allowed = getattr(section, "allowed_numbers", None)
        warn_unusable_entries(raw_allowed)
        client, transport, _dispatcher = build_signal_channel(orch, section)
        if not allowed_numbers_from_config(raw_allowed):
            logger.warning(
                "Signal: allowed_numbers is empty — the channel will REJECT every message "
                "(fail closed). Add your own number or ACI to signal.allowed_numbers "
                "(for Note to Self, add the account's own number)."
            )
        state = getattr(orch, "dashboard_state", None)
        if state is not None:
            client.on_state_change = _state_mirror(state)
            state.register_channel_transport(transport)

        try:
            await transport.connect()  # spawns signal-cli and probes it; raises with its reason
        except RpcTransportError as exc:
            reason = str(exc)
            logger.warning(
                "Signal: signal-cli did not start (%s); retrying in the background.", reason
            )
            client.start_in_background(reason)
            return client
        if state is not None:
            if await client.wait_ready(timeout=READY_TIMEOUT_S):
                state.signal_connected = True
                state.signal_connect_error = ""
            else:
                state.signal_connected = False
                state.signal_connect_error = (client.last_error or "signal-cli not ready")[:120]
        logger.info("Signal channel started for %s (DM only).", redact_identifier(account))
        return client
    except Exception as exc:
        # Not a signal-cli failure (those retry above): a bug or a malformed
        # config object. Reported by type only, and the channel stays off.
        reason = type(exc).__name__
        _report_error(orch, reason)
        logger.exception("Failed to start Signal channel (%s); continuing without it.", reason)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("Signal: closing after a failed start failed", exc_info=True)
        return None
