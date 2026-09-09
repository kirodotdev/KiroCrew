"""WeCom (企业微信) channel startup -- wired into the gateway boot.

``maybe_start_wecom`` is the single guarded entry point. When the channel is
enabled + credentialed it builds the :class:`WeComDispatcher` +
:class:`WeComTransport` + the low-level :class:`WeComClient`, wires the client's
inbound WS frames into ``transport.receive`` (authorize + normalize ->
dispatcher), then opens the outbound WebSocket via ``transport.connect()``.
Failures are logged and swallowed so a WeCom problem never takes down the
gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction +
tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.

``warn_if_channel_uncredentialed`` is the diagnostic companion, generalized
over every collapsed-flag channel: the channel registry's
enabled-only gate never calls a factory when ``_<channel>_enabled`` is False,
so ``_start_channel_transports`` logs each channel's enabled-but-uncredentialed
skip reason through this helper at the start decision point, after
``KIROCREW_READY``. ``warn_if_wecom_uncredentialed`` remains as the
WeCom-shaped wrapper pinning the original contract.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from kiro_crew.config import live
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.wecom.client import WeComClient
from kiro_crew.wecom.transport import WeComTransport, allowed_userids_from_config
from kiro_crew.wecom.transport_dispatch import WeComDispatcher

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the Slack/Telegram path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides, collapsing anything that isn't
    ``auto`` to interactive (deny-by-default on WeCom, which has no buttons).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


def _allowed_userids(orch: "GatewayOrchestrator") -> list[str]:
    """Extract the configured WeCom allow-list userids (filtered).

    Same flattening the live reconfigure path uses; a roster that is not a list
    authorizes nobody at boot (deny-by-default).
    """
    return allowed_userids_from_config(_wecom_section(orch).allowed_users) or []


def _wecom_section(orch: "GatewayOrchestrator") -> Any:
    """The ``wecom`` section in force NOW: the watcher's snapshot, else boot's.

    The factory also runs on an in-process channel restart, by which time the
    orchestrator's boot copy may be stale; the snapshot is what every other
    live reader adopted.
    """
    cfg = live.snapshot() or orch._cfg
    return cfg.wecom


def warn_if_channel_uncredentialed(
    channel_type: str,
    settings_name: str,
    cfg_enabled: bool,
    credential_presence: "Sequence[tuple[str, bool]]",
) -> None:
    """Log WHY a channel will not start when enabled but missing credentials.

    Every ``_<channel>_enabled`` flag is computed as ``cfg.<channel>.enabled
    AND <credential operands>`` (see GatewayOrchestrator), which collapses
    "channel disabled" and "channel enabled but uncredentialed" into one
    boolean -- and the channel registry's enabled-only gate then skips the
    channel factory entirely for BOTH states, so no code inside a factory can
    ever report the difference. This helper is therefore called by
    ``_start_channel_transports`` at the start decision point (after
    ``KIROCREW_READY``, off the boot-path window), once per collapsed-flag
    channel, from the presence checks each flag was computed from.

    ``credential_presence`` holds ``(name, present)`` pairs for exactly the
    operands the channel's enabled-flag predicate reads -- no more (a name that does not
    gate the flag would send the operator to configure something that cannot
    start the channel) and no fewer. When the operator enabled the channel but
    at least one operand is absent, it emits exactly one WARNING (visible at
    the default log level) on the channel's own gateway logger, naming the
    missing credential NAME(s), never values; when the channel is disabled, or
    fully credentialed, it stays completely silent. Callers pass booleans so
    the diagnostic never receives credential values.
    """
    if not cfg_enabled or all(present for _, present in credential_presence):
        return
    missing = " and ".join(name for name, present in credential_presence if not present)
    channel_logger = logging.getLogger(f"kiro_crew.{channel_type}.gateway")
    # The rule keys on the word "credential" in the format string; the call
    # logs only the MISSING credential variable name(s) and a static
    # remediation hint — no credential value is among the format arguments,
    # and the tests pin that present values never appear.
    channel_logger.warning(  # nosemgrep: python-logger-credential-disclosure
        "%s: enabled but not credentialed (missing %s) — skipping. "
        "Set the missing credential(s) in the dashboard %s settings.",
        channel_type,
        missing,
        settings_name,
    )


def warn_if_wecom_uncredentialed(cfg_enabled: bool, bot_id: str, secret: str) -> None:
    """WeCom-shaped wrapper over :func:`warn_if_channel_uncredentialed`.

    Preserves the public contract (pinned by
    ``test_wecom_gateway.py::TestSkipReasonWarning``): exactly one WARNING on
    this module's logger naming the missing credential name(s)
    (``WECOM_BOT_ID`` / ``WECOM_SECRET``), values never logged, silence when
    disabled or fully credentialed. Production routes through the
    channel table in ``_start_channel_transports``, which
    feeds the generic helper the same ``(name, present)`` pairs.
    """
    warn_if_channel_uncredentialed(
        "wecom",
        "WeCom",
        cfg_enabled,
        (("WECOM_BOT_ID", bool(bot_id)), ("WECOM_SECRET", bool(secret))),
    )


async def maybe_start_wecom(orch: "GatewayOrchestrator") -> "WeComClient | None":
    """Start the WeCom channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None. The transport + dispatcher stay alive via the client's handler
    references.

    The enabled-but-uncredentialed diagnostic does NOT live here: the channel
    registry only calls this factory when ``_wecom_enabled`` is already true,
    so the skip reason is logged by :func:`warn_if_channel_uncredentialed`
    from ``_start_channel_transports`` instead.
    """
    if not getattr(orch, "_wecom_enabled", False):
        return None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("ALL_PROXY")
        )
        dispatcher = WeComDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            owner_id=orch._owner_id,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        wecom_cfg = _wecom_section(orch)
        client = WeComClient(
            bot_id=orch._wecom_bot_id,
            secret=orch._wecom_secret,
            ws_url=wecom_cfg.ws_url,
            proxy=proxy,
        )
        transport = WeComTransport(
            client,
            allowed_users=_allowed_userids(orch),
            allow_all=bool(wecom_cfg.allow_all_users),
            owner_id=orch._owner_id,
            dispatch=dispatcher.handle_message,
        )
        # Inbound: client WS frame -> transport.receive (authorize + normalize)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client
        # The dispatcher's config applier pushes reloaded allow-list fields at
        # the transport; wired here for the same construction-cycle reason.
        dispatcher.transport = transport

        # Keep the settings badge truthful: connect() only SCHEDULES the WS
        # loop, so "started" proves nothing about the credentials. The client
        # reports live connection transitions (connected + subscribed /
        # connect failure / immediate close on bad creds / server kick)
        # through on_status; start not-connected and let the first transition
        # flip it. This is the compensating control for skipping save-time
        # credential verification in the config API. Wired BEFORE connect()
        # so the very first transition cannot fire into a missing callback
        # (the dedupe would then swallow the re-report forever).
        if orch.dashboard_state is not None:
            state = orch.dashboard_state
            state.wecom_connected = False
            state.wecom_connect_error = ""

            def _on_status(healthy: bool, reason: str) -> None:
                state.wecom_connected = healthy
                state.wecom_connect_error = "" if healthy else reason[:120]

            client.on_status = _on_status

        await transport.connect()  # opens the outbound WS connect/serve loop
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
        logger.info("WeCom (企业微信) channel started (transport path).")
        return client
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.wecom_connected = False
            orch.dashboard_state.wecom_connect_error = f"{type(exc).__name__}: {exc}"[:120]
        logger.exception("Failed to start WeCom channel; continuing without it.")
        return None
