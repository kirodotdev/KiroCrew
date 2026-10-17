"""Observe-only defaults.

These two adapters are what make a fresh install both useful and harmless.

``NoopActionSink`` accepts every action and performs none of them, recording what
*would* have happened. It is the default sink, so an install that has not been
granted a write scope cannot write anywhere — and the board still shows the full
proposed-action flow, which is what lets a user watch the agent be right a few
times before granting real authority.

``AlwaysOnRotationSource`` reports permanently on-shift. A solo operator has no
rotation, and the on-shift automation tier must degrade to always-on rather than
never firing — the alternative is an app that silently does nothing for exactly
the user most likely to install it.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    VALID_ACTIONS,
    Signal,
)
from kiro_crew.apps.builtins.ops_mission_control.backend.providers.base import (
    ActionResult,
    ShiftStatus,
)

logger = logging.getLogger(__name__)


class NoopActionSink:
    """Default sink: records intent, performs nothing."""

    id = "noop"
    display_name = "Observe only"
    detail = "Records what would be done without calling any provider."
    config_fields: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()

    def configured(self) -> bool:
        # Always available — this is the fallback that guarantees there is always
        # a sink to route to, so a proposal never fails for lack of one.
        return True

    def supported_actions(self) -> frozenset[str]:
        return VALID_ACTIONS

    async def execute(self, signal: Signal, action: str, payload: dict[str, Any]) -> ActionResult:
        note = str(payload.get("note", ""))[:200]
        logger.info(
            "ops-mission-control: observe-only, would %s %s%s",
            action,
            signal.id,
            f" ({note})" if note else "",
        )
        return ActionResult(
            ok=True,
            action=action,
            # ``ok`` here means "we successfully did nothing", and post-action verification
            # cannot tell that from a real provider write — see ``ActionResult.simulated``.
            simulated=True,
            detail=f"observe-only: would {action} {signal.id}",
        )


class AlwaysOnRotationSource:
    """Default rotation: always on shift.

    Marked ``is_fallback`` so ``Registry.resolve_shift`` can tell a *default* from a
    real rotation. Without that distinction this adapter silently defeats every other
    rotation source: it is always configured and always reports on-shift, and
    ``resolve_shift`` returns the first on-shift answer it finds — so a PagerDuty or
    schedule-file source correctly reporting "someone else is on call" could never be
    heard. Verified before fixing: a real off-shift source plus this one resolved to
    ``on_shift=True``.
    """

    id = "always-on"
    display_name = "Always on shift"
    detail = "No rotation configured — the on-shift tier stays armed."
    config_fields: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()

    #: This is a floor, not an opinion. ``resolve_shift`` consults fallbacks only when
    #: no real source could answer.
    is_fallback = True

    def configured(self) -> bool:
        return True

    async def on_shift(self) -> ShiftStatus:
        return ShiftStatus(on_shift=True, who="", until="")
