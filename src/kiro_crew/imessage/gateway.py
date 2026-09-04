"""iMessage channel startup -- wired into the gateway boot.

``maybe_start_imessage`` is the single guarded entry point. When the channel is
enabled it builds the :class:`IMessageDispatcher` + :class:`IMessageTransport` +
the low-level :class:`IMessageClient`, wires the client's watch notifications
into ``transport.receive`` (own-message suppression + group fail-close +
authorize + normalize -> dispatcher), then connects via ``transport.connect()``.
Failures are logged and swallowed so an iMessage problem never takes down the
gateway.

Two refusals are deliberate and reported rather than retried:

* **Not macOS.** There is no iMessage outside Apple's stack, and the bridge is a
  macOS binary. A hosted relay would satisfy the letter of the feature while
  destroying its point, so the channel simply does not start elsewhere.
* **Empty allow-list.** Anyone who knows the user's number can send to it, so an
  unconfigured allow-list means the channel is reachable but answers nobody.
  That is the correct fail-closed behaviour, and it is worth a warning because
  the symptom ("it never replies") does not look like a configuration problem.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.imessage.client import IMessageClient
from kiro_crew.imessage.transport import IMessageTransport
from kiro_crew.imessage.transport_dispatch import IMessageDispatcher
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.platform_compat import IS_MACOS

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the other channels).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default unless a decider/hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_imessage(orch: "GatewayOrchestrator") -> "IMessageClient | None":
    """Start the iMessage channel if enabled and the host can serve it.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references.
    """
    if not getattr(orch, "_imessage_enabled", False):
        return None
    if not IS_MACOS:
        # v1 requires the gateway to run ON the Messages host: a remote-shell
        # wrapper can read chats but its outbound sends fail with an AppleEvents
        # authorization error, because the Automation grant is recorded against
        # the remote-shell server process, which macOS exposes no grantable
        # toggle for. Refusing is better than a send path we cannot promise.
        message = "iMessage requires macOS with Messages signed in; channel not started"
        logger.warning("%s (platform is not darwin).", message)
        if orch.dashboard_state is not None:
            orch.dashboard_state.imessage_connect_error = message[:120]
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        cfg = orch._cfg

        allowed_handles: list[str] = [h for h in (cfg.imessage.allowed_handles or []) if h]
        if not allowed_handles:
            logger.warning(
                "iMessage: allowed_handles is empty — anyone who knows this Mac's "
                "handle can send to it, so the channel will REJECT every message "
                "(fail closed). Add your own phone number or Apple ID email to "
                "imessage.allowed_handles to enable."
            )

        dispatcher = IMessageDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=cfg,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = IMessageClient(
            db_path=cfg.imessage.db_path,
            service=cfg.imessage.service,
        )
        transport = IMessageTransport(
            client, allowed_handles=allowed_handles, dispatch=dispatcher.handle_message
        )
        # Inbound: bridge watch notifications -> transport.receive (suppress own
        # messages, fail closed on groups, authorize + normalize) ->
        # dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        await transport.connect()  # spawns the bridge + opens the watch
        if orch.dashboard_state is not None:
            state = orch.dashboard_state
            state.register_channel_transport(transport)

            # Keep the status badge truthful across the channel's lifetime: the
            # watch dropping (database unavailable, bridge exit) flips it back
            # off with the reason.
            def _on_state(connected: bool, error: str) -> None:
                state.imessage_connected = connected
                state.imessage_connect_error = error[:120]

            client.on_state_change = _on_state
            if await client.wait_ready(timeout=15.0):
                state.imessage_connected = True
                state.imessage_connect_error = ""
            else:
                state.imessage_connected = False
                state.imessage_connect_error = (
                    client.last_error
                    or "bridge not ready within 15s (check imsg install and Full Disk Access)"
                )
        logger.info("iMessage channel started (local bridge, DM only).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.imessage_connect_error = type(exc).__name__[:120]
        logger.exception("Failed to start iMessage channel; continuing without it.")
        return None
