"""The one dashboard note that says a Kiro CLI pick now runs on the KAS engine.

kiro-cli serves ``acp`` on its v3 (KAS) engine by default, and a stored Kiro CLI pick
(``agent.acp_backend: "kiro"``) resolves to KAS while kiro-cli offers that engine
(``agent_sdk.backends.KIRO_SELECTION_BACKEND``). ``config.json`` keeps saying
``"kiro"``, so the user is told once, in the bell, and the note opens the Agent
Harness panel, whose one button saves KAS as the setting.

Delivered after the gateway's KAS floor has answered, and only when that answer
confirmed v3: a kiro-cli too old to offer it, a probe that could not run and the
bundle route all leave the pick on the v2 launch, and get no note. Claimed with an ``O_EXCL``
marker under the data home before the note goes out: a second gateway, a restart or
a crash after the claim never announces it again, and a claim that cannot be written
announces nothing. See the a-kiro-cli-choice-runs-on-kas entry in docs/decisions.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Claimed once per data home, right before the note is delivered.
MARKER_FILENAME = "announced-kiro-cli-on-kas"

NOTE_KIND = "agent"
NOTE_CHANNEL = "system.agent"
NOTE_TITLE = "Kiro CLI now runs on Kiro Agent"
NOTE_BODY = (
    "Kiro CLI now runs on Kiro Agent, so new sessions already use it. Open Agent "
    "Harness to save Kiro Agent as your setting."
)
#: The Agent Harness panel, where the note's one-click fix lives.
NOTE_URL = "/settings/agent"

#: Strong reference to the scheduled delivery: the loop holds tasks weakly.
_TASK: "asyncio.Task[None] | None" = None


def kiro_selection_runs_on_kas() -> bool:
    """Whether the stored pick is Kiro CLI and a load resolves it to KAS. Blocking."""
    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_document import (
        acp_backend_overlay_owned,
        acp_backend_stored_as_kiro,
    )

    if not acp_backend_stored_as_kiro():
        return False
    if acp_backend_overlay_owned():
        # The panel's save writes ``config.json`` and the overlay wins over it, so
        # its button would report a save that changes nothing: offer nothing.
        return False
    # The loaded field is the one answer: it applies every condition of the
    # remap (a confirmed v3, KAS selectable, not the bundle route).
    return KiroCrewConfig.load().agent.acp_backend == ACP_BACKEND_KAS


def _claim() -> bool:
    """Create the marker exclusively. ``True`` only for the caller that made it."""
    from kiro_crew.config.loader import config_dir

    path = config_dir() / MARKER_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        logger.warning("could not claim the Kiro CLI on KAS notice", exc_info=True)
        return False
    os.close(fd)
    return True


def prepare_note() -> dict[str, Any] | None:
    """The note when it is owed and now claimed, else ``None``. Blocking; never raises."""
    try:
        if not kiro_selection_runs_on_kas():
            return None
        if not _claim():
            return None
    except Exception:  # noqa: BLE001 - no note is the safe direction
        logger.warning("could not work out the Kiro CLI on KAS notice", exc_info=True)
        return None
    return {"title": NOTE_TITLE, "body": NOTE_BODY, "url": NOTE_URL}


async def deliver_when_floor_lands(state: Any, floor: "asyncio.Future[Any] | None") -> bool:
    """Wait for the KAS floor's verdict, then deliver the note if it is owed.

    Returns whether a note went out. The floor's own failure is contained there and
    here alike: a failed probe leaves KAS selected, and the note then says so truly.
    """
    if floor is not None:
        # ``wait`` neither raises the floor's own failure or cancellation nor
        # cancels the floor when this task is cancelled.
        await asyncio.wait({floor})
    note = await asyncio.to_thread(prepare_note)
    if note is None:
        return False
    try:
        state.notify(NOTE_KIND, note["title"], note["body"], url=note["url"], channel=NOTE_CHANNEL)
    except Exception:  # noqa: BLE001 - the claim is spent; the panel still shows the offer
        logger.warning("could not deliver the Kiro CLI on KAS notice", exc_info=True)
        return False
    return True


def schedule(state: Any, floor: "asyncio.Future[Any] | None") -> "asyncio.Task[None]":
    """Run :func:`deliver_when_floor_lands` in the background. Call on the loop."""
    global _TASK

    async def _run() -> None:
        try:
            await deliver_when_floor_lands(state, floor)
        except Exception:  # noqa: BLE001 - a missing note must not reach the gateway
            logger.warning("Kiro CLI on KAS notice failed", exc_info=True)

    _TASK = asyncio.create_task(_run(), name="kiro-cli-on-kas-notice")
    return _TASK
