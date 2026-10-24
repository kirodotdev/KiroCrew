"""Jev's effort for a routed dashboard turn, pushed live inside the session's range, and
the owner's own level put back when the turn ends."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.llm_helpers import pick_epoch_host
from kiro_crew.providers.acp_model_effort import JEV_ROUTE_EFFORT_ATTR as _JEV_ROUTE_EFFORT_ATTR

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import logger


async def _apply_jev_route_effort(
    slot: Any,
    client: Any,
    session_key: str,
    routed: dict[str, Any],
    model_route: Any,
    baseline_model: str,
) -> None:
    """Push the tier's effort to the session and name it on *routed*. Never raises.

    Called after any model switch, because ``set_model`` re-applies the target's
    stored level. The push is live only, so the owner's saved level is never touched.
    Only a slot the owner put on ``Auto (Jev)`` has a range to route inside.
    """
    if not getattr(slot, "jev_route", False):
        return
    level = model_route.tier_effort(str(routed.get("tier") or ""), slot.jev_range)
    if not level:
        return
    host = pick_epoch_host(client)
    marked = getattr(host, _JEV_ROUTE_EFFORT_ATTR, None)
    try:
        if not client.supports_effort():
            return
        # The owner's level for the model the session ran on before routing: what the
        # record names, and what the restore falls back to.
        baseline = client.owner_effort(baseline_model)
        if not baseline:
            routed["effort_skipped"] = model_route.EFFORT_SKIPPED_BASELINE_UNKNOWN
            return
        # Marked before the await: a Stop during the push may land after the level.
        setattr(host, _JEV_ROUTE_EFFORT_ATTR, baseline)
        if not await client.push_live_effort(level):
            setattr(host, _JEV_ROUTE_EFFORT_ATTR, marked)
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Kept: a push that raised may still have landed, and an extra restore is harmless.
        logger.warning(
            "model.route: pushing effort %r failed for slot %s (%s); the turn keeps "
            "the session's level",
            level,
            slot.key,
            type(exc).__name__,
        )
        return
    routed["effort_chosen"] = level
    routed["baseline_effort"] = baseline


async def _restore_jev_route_effort(slot: Any, client: Any) -> None:
    """Put the owner's level back on a session a routed effort left live. Never raises.

    Runs when a turn ends, before its session is released, and before the next turn,
    which retries a restore that failed. The level is the owner's for the live model,
    which the dropdown shows, else the one captured when Jev's level went live.
    """
    host = pick_epoch_host(client)
    marked = getattr(host, _JEV_ROUTE_EFFORT_ATTR, None)
    if not isinstance(marked, str) or not marked:
        return
    try:
        if not await client.push_live_effort(client.owner_effort() or marked):
            return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "model.route: restoring the owner's effort failed for slot %s (%s)",
            slot.key,
            type(exc).__name__,
        )
        return
    setattr(host, _JEV_ROUTE_EFFORT_ATTR, None)
