"""Feishu (Lark/飞书) channel startup -- wired into the gateway boot.

``maybe_start_feishu`` is the single guarded entry point.  When the channel
is enabled + credentialed it builds the :class:`FeishuDispatcher` +
:class:`FeishuTransport` + the low-level :class:`LarkClient`, wires the
client's inbound WS frames into ``transport.receive`` (authorise + normalise
-> dispatcher), then opens the WebSocket via ``transport.connect()``.
Failures are logged and swallowed so a Feishu problem never takes down the
gateway.

The turn itself runs on the shared ``TurnDriver`` (credential/exfil redaction
+ tool-approval ladder + SEL audit) via the dispatcher -- no hand-rolled loop.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from typing import TYPE_CHECKING

from kiro_crew import extras, sdk_extras
from kiro_crew.config import live
from kiro_crew.feishu.client import LarkClient
from kiro_crew.feishu.transport import FeishuTransport
from kiro_crew.feishu.transport_dispatch import FeishuDispatcher
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

#: Every lark-oapi module LarkClient imports, preloaded off the event loop.
_LARK_MODULES = ("lark_oapi", "lark_oapi.api.im.v1", "lark_oapi.ws")


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    """Resolve the transport approval mode (mirrors the WeCom / Telegram path).

    YOLO -> auto-approve; otherwise the CLI ``--approval`` override or the
    configured ``agent.approval_mode`` decides.  Feishu has no interactive
    buttons, so anything other than ``auto`` collapses to interactive
    (deny-by-default unless a hook approves).
    """
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


async def maybe_start_feishu(orch: "GatewayOrchestrator") -> "LarkClient | None":
    """Start the Feishu channel if enabled + credentialed; else no-op.

    Returns the running client (so the gateway can ``close()`` it on shutdown)
    or None.  The transport + dispatcher stay alive via the client's handler
    references.  ``app_id`` / ``app_secret`` are read from ``orch`` (set in
    ``GatewayOrchestrator.__init__`` from env / credentials store).
    """
    if not getattr(orch, "_feishu_enabled", False):
        return None
    try:
        assert orch.sessions is not None and orch.ctx_builder is not None

        # lark-oapi loads about 11,000 modules on import, which can take tens of
        # seconds on a slow filesystem. LarkClient imports it inline, so load it
        # on a worker thread first: on the loop it would stall the gateway long
        # enough for the loop-stall watchdog to restart it. Every later import
        # is then a sys.modules hit. A missing extra still raises ImportError.
        # On the desktop app the SDK lives in the data home, installed from
        # Settings (``sdk_extras``); put it on sys.path first.
        await asyncio.to_thread(sdk_extras.activate)
        for module in _LARK_MODULES:
            await asyncio.to_thread(importlib.import_module, module)

        allowed_open_ids: list[str] = list(getattr(orch, "_feishu_allowed_open_ids", []) or [])
        if not allowed_open_ids:
            logger.warning(
                "Feishu: feishu.allowed_open_ids is empty — the bot is globally "
                "reachable but will REJECT all messages (fail closed). Add your "
                "Feishu open_id to feishu.allowed_open_ids to enable."
            )

        dispatcher = FeishuDispatcher(
            sessions=orch.sessions,
            ctx_builder=orch.ctx_builder,
            cfg=orch._cfg,
            agent=None,
            conv_log=getattr(orch, "conv_log", None),
            approval_mode=_resolve_approval_mode(orch),
        )
        client = LarkClient(
            app_id=orch._feishu_app_id,
            app_secret=orch._feishu_app_secret,
        )
        # With the config watcher running, the boot copy may be older than the
        # config in force: an edit that landed during the preload found no
        # transport and was dropped. Seed the transport from the adopted
        # section instead, or with nobody authorized when the loader discarded
        # that section, and let the replay below reconcile. Seeding (rather
        # than starting empty) keeps the replay from auditing grants nobody
        # made.
        snap = live.snapshot()
        watched = snap is not None
        if snap is None:
            seed_open_ids = allowed_open_ids
            seed_allow_group = bool(getattr(orch, "_feishu_allow_group", False))
            seed_group_ids = list(getattr(orch, "_feishu_allowed_group_ids", []) or [])
        elif "feishu" in snap.degraded_sections:
            seed_open_ids, seed_allow_group, seed_group_ids = [], False, []
        else:
            seed_open_ids = list(snap.feishu.allowed_open_ids)
            seed_allow_group = bool(snap.feishu.allow_group)
            seed_group_ids = list(snap.feishu.allowed_group_ids)
        transport = FeishuTransport(
            client,
            allowed_open_ids=seed_open_ids,
            allow_group=seed_allow_group,
            allowed_group_ids=seed_group_ids,
            dispatch=dispatcher.handle_message,
        )
        # Inbound: client WS frame -> transport.receive (authorise + normalise)
        # -> dispatcher.handle_message (drive the turn on the shared TurnDriver).
        # set_message_handler avoids the client<->transport construction cycle.
        client.set_message_handler(transport.receive)
        dispatcher.client = client
        # The dispatcher's config applier pushes reloaded authorization fields at
        # the transport; wired after construction, like ``client``.
        dispatcher.transport = transport
        # No await separates this from the wiring above, so every later edit
        # reaches the transport through the dispatcher's applier. A degraded
        # section is deferred, and the transport keeps authorizing nobody until
        # the applier accepts the section (the loader keeps a section it once
        # saw degraded marked so until a restart).
        if watched:
            live.watch().replay(dispatcher._config_sub)
        dispatcher.dashboard_state = orch.dashboard_state

        # Keep the settings badge truthful: connect() only SPAWNS the receiver
        # thread, so "started" proves nothing about the credentials. The client
        # reports liveness transitions through on_state_change; start
        # not-connected and let the first transition flip it. Wired BEFORE
        # connect() so the very first transition cannot fire into a missing
        # callback (the dedupe would then swallow the re-report forever).
        if orch.dashboard_state is not None:
            state = orch.dashboard_state
            state.feishu_connected = False
            state.feishu_connect_error = ""

            def _on_status(healthy: bool, reason: str) -> None:
                state.feishu_connected = healthy
                state.feishu_connect_error = "" if healthy else reason[:120]

            client.on_state_change = _on_status

        await transport.connect()  # spawns the WS daemon thread
        if orch.dashboard_state is not None:
            orch.dashboard_state.register_channel_transport(transport)
        logger.info("Feishu (飞书/Lark) channel started (app_id=%s).", orch._feishu_app_id)
        return client
    except ImportError as exc:
        # The one failure a user can fix without reading a log: lark-oapi is an
        # optional extra, and without it LarkClient's constructor raises here
        # before anything else runs. The badge names the missing package and
        # the restart; the install command is the Settings card's, which names
        # this gateway's interpreter. The log line keeps the short pip hint.
        if orch.dashboard_state is not None:
            orch.dashboard_state.feishu_connected = False
            orch.dashboard_state.feishu_connect_error = extras.missing_at_start_status("lark-oapi")
        logger.error(
            "Feishu channel needs the lark-oapi extra (%s); continuing without it: %s",
            extras.install_hint("feishu"),
            exc,
        )
        _schedule_sdk_install(orch)
        return None
    except Exception as exc:
        if orch.dashboard_state is not None:
            orch.dashboard_state.feishu_connected = False
            orch.dashboard_state.feishu_connect_error = f"{type(exc).__name__}: {exc}"[:120]
        logger.exception("Failed to start Feishu channel; continuing without it.")
        return None


def _schedule_sdk_install(orch: "GatewayOrchestrator") -> None:
    """On the desktop app, install the missing SDK in the background, then start Feishu.

    Reached only for an enabled, credentialed channel (``maybe_start_feishu``
    returns earlier otherwise), so this is a user who already uses Feishu, and
    their channel must not stay off just because the bundle lacks the SDK. The install runs off the event loop and never holds up
    the other channels or the dashboard; once it lands, the channel is started
    through ``restart_channel``, as a config edit would. One attempt per start:
    a failure leaves the Settings Install button and the badge as they are.
    """
    if getattr(orch, "_feishu_sdk_install_task", None) is not None:
        return
    restart = getattr(orch, "restart_channel", None)
    if restart is None:
        return

    async def _run() -> None:
        try:
            if not await asyncio.to_thread(sdk_extras.install_supported, "feishu"):
                return
            logger.info("Feishu: installing the Feishu SDK for the enabled channel")
            await asyncio.to_thread(sdk_extras.install_extra, "feishu")
        except sdk_extras.InstallError as exc:
            logger.warning(
                "Feishu: automatic SDK install failed; press Install under"
                " Settings → Messaging Channels → Feishu: %s",
                exc,
            )
            return
        except Exception:
            logger.exception("Feishu: automatic SDK install failed")
            return
        logger.info("Feishu: SDK installed; starting the channel")
        try:
            await restart("feishu")
        except Exception:
            logger.exception("Feishu: starting the channel after the SDK install failed")

    # Held on the orchestrator so the task is not collected mid-install.
    setattr(
        orch, "_feishu_sdk_install_task", asyncio.create_task(_run(), name="feishu-sdk-install")
    )
