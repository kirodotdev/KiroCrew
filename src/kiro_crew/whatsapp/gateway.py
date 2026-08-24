"""WhatsApp channel startup, wired into the gateway boot."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from kiro_crew.config.paths import data_home
from kiro_crew.messaging.driver import APPROVAL_AUTO, APPROVAL_INTERACTIVE
from kiro_crew.whatsapp.client import (
    STATE_CONNECTED,
    WhatsAppClient,
    default_db_path,
    neonize_available,
)
from kiro_crew.whatsapp.transport import WhatsAppTransport
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)


def _resolve_approval_mode(orch: "GatewayOrchestrator") -> str:
    if getattr(orch, "_approval_mode", None) == "yolo":
        return APPROVAL_AUTO
    mode = getattr(orch, "_approval_mode", None) or orch._cfg.agent.approval_mode
    return APPROVAL_AUTO if mode == APPROVAL_AUTO else APPROVAL_INTERACTIVE


def _configured_group_jids(groups: object) -> list[str]:
    """The non-empty ``jid`` of each group entry, keyed exactly as the gate keys it.

    ``GroupGate`` indexes its entries by ``str(entry["jid"]).strip()`` and looks
    them up by exact string, so this reads the same value the same way: a JID that
    differs from the gate's key by case or a ``:device`` suffix is one the gate
    never matches either, and normalizing here would call it fine.
    """
    if not isinstance(groups, list):
        return []
    out: list[str] = []
    for entry in groups:
        if not isinstance(entry, dict):
            continue
        jid = str(entry.get("jid", "")).strip()
        if jid:
            out.append(jid)
    return out


async def _check_configured_groups(client: "WhatsAppClient", groups: object) -> None:
    """Warn once about configured group JIDs the linked account is not a member of.

    A group entry whose JID the account cannot resolve is INVISIBLE rather than
    broken: the gate drops every message from a JID it does not hold, so a
    mistyped entry and a group nobody has written in look identical from the
    operator's chair, and the only lever they have is a JID no surface shows them.

    Runs off the CONNECTED transition and never on the start path, because
    ``client.connect()`` returns once the attempt is merely underway: a check taken
    there finds no joined groups yet and would name every configured one.
    """
    configured = _configured_group_jids(groups)
    if not configured:
        return
    try:
        joined = await client.list_groups()
    except Exception:  # noqa: BLE001 (a diagnostic must never raise out of a task)
        logger.warning("whatsapp: group membership check failed", exc_info=True)
        return
    if not joined:
        # ``list_groups()`` answers ``[]`` both for "this account is in no groups"
        # and for a swallowed ``get_joined_groups`` failure, so an empty answer
        # cannot tell a stale JID from a probe that never ran. Naming every
        # configured group on a transient API failure is what teaches an operator
        # to ignore the warning, so silence is the trustworthy branch.
        logger.debug("whatsapp: group membership check skipped, no joined groups reported")
        return
    present = {str(g.get("jid", "")).strip() for g in joined if isinstance(g, dict)}
    missing = [jid for jid in configured if jid not in present]
    if not missing:
        return
    # ONE aggregated line: a warning per group buries the rest of the channel's
    # startup log and reads as several unrelated faults instead of one stale list.
    logger.warning(
        "whatsapp: %d of %d configured group(s) are not groups this account is in, "
        "so messages there are ignored: %s. Re-pick them in Settings > Channels.",
        len(missing),
        len(configured),
        ", ".join(missing),
    )


async def maybe_start_whatsapp(orch: "GatewayOrchestrator") -> "WhatsAppClient | None":
    """Start the WhatsApp channel if enabled; else no-op."""
    if not getattr(orch, "_whatsapp_enabled", False):
        return None
    state = orch.dashboard_state
    if not neonize_available():
        from kiro_crew.whatsapp.client import MISSING_EXTRA_HINT

        logger.warning("whatsapp: %s", MISSING_EXTRA_HINT)
        if state is not None:
            state.whatsapp_connected = False
            state.whatsapp_connect_error = MISSING_EXTRA_HINT[:120]
        return None

    try:
        assert orch.sessions is not None and orch.ctx_builder is not None
        cfg = orch._cfg.whatsapp
        # ALWAYS the default path. The store holds whatsmeow's device keys, which
        # are the whole credential: anything that reads them can act as the
        # operator on WhatsApp. Its protection is a PATH match on the sensitive
        # keystone (`whatsapp` under the data home), so an operator-supplied
        # location silently moves the credential outside the one thing stopping a
        # prompt-injected agent from reading it. A relocation option is not worth
        # a credential whose protection depends on where it happens to sit.
        client = WhatsAppClient(str(default_db_path(data_home())))
        dispatcher = WhatsAppDispatcher(
            orch._cfg,
            orch.sessions,
            orch.ctx_builder,
            approval_mode=_resolve_approval_mode(orch),
        )
        dispatcher.client = client
        dispatcher.conv_log = getattr(orch, "conv_log", None)
        transport = WhatsAppTransport(
            client,
            dispatcher.handle_message,
            dm_policy=cfg.dm_policy,
            allowed_wa_ids=list(cfg.allowed_wa_ids),
            groups=list(cfg.groups),
        )
        dispatcher.transport = transport

        loop = asyncio.get_running_loop()
        checked_groups = False
        # Strong references to the in-flight check: the loop keeps only a weak one,
        # so a task nobody awaits can be collected mid-flight.
        pending: set[asyncio.Task] = set()

        def _start_group_check() -> None:
            task = loop.create_task(
                _check_configured_groups(client, cfg.groups),
                name="whatsapp-group-check",
            )
            pending.add(task)
            task.add_done_callback(pending.discard)

        # ONE observer for both jobs. ``on_state_change`` is a single slot, so a
        # second assignment would silently drop whichever was installed first.
        def _on_state(new_state: str, detail: str) -> None:
            nonlocal checked_groups
            if state is not None:
                state.whatsapp_connected = new_state == STATE_CONNECTED
                state.whatsapp_connect_error = (
                    "" if new_state == STATE_CONNECTED else f"{new_state}: {detail}"[:120]
                )
            if new_state != STATE_CONNECTED or checked_groups:
                return
            checked_groups = True
            # Scheduled rather than awaited, so the round trip lands after the
            # remaining channels have started instead of inside the sequence that
            # starts them. ``call_soon_threadsafe`` because the neonize event that
            # drives this transition need not arrive on the gateway's own thread.
            try:
                loop.call_soon_threadsafe(_start_group_check)
            except RuntimeError:
                # The loop is gone (shutdown raced the transition). This is a
                # diagnostic, so losing it must never surface as an error.
                logger.debug("whatsapp: group membership check not scheduled, loop closed")

        client.on_state_change = _on_state
        if state is not None:
            state.register_channel_transport(transport)
        await transport.connect()
        logger.info("WhatsApp channel started (state=%s).", client.state)
        return client
    except Exception as exc:
        logger.exception("Failed to start WhatsApp channel; continuing without it.")
        if state is not None:
            state.whatsapp_connected = False
            state.whatsapp_connect_error = str(exc)[:120]
        return None
