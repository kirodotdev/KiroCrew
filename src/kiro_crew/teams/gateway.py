"""Microsoft Teams channel startup -- wired into the gateway boot.

``maybe_start_teams`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`TeamsDispatcher` +
:class:`TeamsTransport` + the low-level :class:`TeamsClient`, wires the client's
``on_activity`` as the late-bound handler for the pre-registered
``POST /api/messaging/teams`` route (aiohttp freezes routes at startup, so the
route lives in ``dashboard/server.py`` and dispatches through
``state.teams_on_activity``), and primes the outbound app-credential token.
Failures are logged and swallowed so a Teams problem never takes down the
gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.teams.client import HAS_JWT, TeamsClient
from kiro_crew.teams.transport import TeamsTransport
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Slack/Webex path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default unless a decider/hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_teams(orch: "GatewayOrchestrator") -> "TeamsClient | None":
    """Start the Teams channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references and ``state.teams_on_activity``.
    """
    if not getattr(orch, "_teams_enabled", False):
        return None
    app_id = getattr(orch, "_teams_app_id", "")
    app_password = getattr(orch, "_teams_app_password", "")
    if not (app_id and app_password):
        return None

    if not HAS_JWT:
        msg = "PyJWT not installed (pip install 'kirocrew[teams]')"
        logger.error(
            "Teams channel is enabled but PyJWT is missing; inbound JWT "
            "validation is impossible. Install the extra: pip install "
            "'kirocrew[teams]'. Skipping Teams."
        )
        if orch.dashboard_state is not None:
            orch.dashboard_state.teams_connect_error = msg
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        allowed_emails: set[str] = {
            e.lower() for e in (getattr(orch, "_teams_allowed_emails", []) or []) if e
        }
        if not allowed_emails:
            logger.warning(
                "Teams: allowed_emails is empty — the bot is reachable by anyone "
                "in the org but will REJECT all messages (fail closed). Add your "
                "Azure AD UPN/email to teams.allowed_emails to enable."
            )

        dispatcher = TeamsDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = TeamsClient(
            app_id=app_id,
            app_password=app_password,
            tenant_id=getattr(orch, "_teams_tenant_id", ""),
        )
        transport = TeamsTransport(
            client, allowed_emails=allowed_emails, dispatch=dispatcher.handle_message
        )
        # Inbound: pre-registered webhook route -> state.teams_on_activity ->
        # client.on_activity (JWT validate) -> transport.receive (scope gate +
        # authorize + normalize) -> dispatcher.handle_message (shared TurnDriver).
        client.set_message_handler(transport.receive)
        dispatcher.client = client

        if orch.dashboard_state is not None:
            state = orch.dashboard_state
            # Late-bind the webhook handler now that the client exists.
            state.teams_on_activity = client.on_activity
            state.register_channel_transport(transport)

            def _on_state(connected: bool, error: str) -> None:
                state.teams_connected = connected
                state.teams_connect_error = error[:120]

            client.on_state_change = _on_state

        # Prime the outbound token to validate credentials; sets the status
        # badge via on_state_change. Never raises (contained in connect()).
        await client.connect()
        logger.info("Teams channel started (self-hosted webhook path).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.teams_connect_error = type(exc).__name__[:120]
        logger.exception("Failed to start Teams channel; continuing without it.")
        return None
