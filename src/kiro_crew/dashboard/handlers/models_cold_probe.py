"""Fill a cold model list without waiting for the first chat.

A harness whose models come only from its own ``session/new`` answer (Codex and
the other advertised-selection harnesses, and KAS from Crew's engine bundle) has
nothing to list until a session of it starts. ``GET /api/models`` then answers
``auto`` alone, and the dashboard keeps that 200 until something tells it to read
again, so the picker shows only Auto until the user sends a first message.

On such a cold read the handler calls :func:`start_cold_model_probe`. It opens one
throwaway background session on the shared runtime in a background task, without
holding up the request, and destroys it at once. That session's ``session/new``
records the harness's models through the same capture a chat session uses
(``AcpRuntime.create_session`` -> ``store_session_config`` and the advertised
cache feed). Then a ``models_changed`` frame tells every dashboard to read the
list again. Nothing persistent is registered, so a later harness switch finds no
leftover background entry of this one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: The dashboard frame that makes every picker read ``/api/models`` again.
MODELS_CHANGED_EVENT = "models_changed"

#: Where the running probe task and the last start per backend are kept: on the
#: dashboard state, the one object that lives as long as the gateway's dashboard
#: (the aiohttp app is frozen).
_TASK_ATTR = "_cold_model_probe_task"
_STARTED_ATTR = "_cold_model_probe_started"

#: One probe per backend per this many seconds. A harness that advertises
#: nothing stays cold after a probe; the re-read its frame causes then answers
#: cold again without starting another session until this has passed.
COLD_PROBE_COOLDOWN_SECS = 300.0

#: The clock the cooldown reads, bound here so a test can set it.
_now = time.monotonic


def start_cold_model_probe(state: Any, backend: str) -> bool:
    """Start one background probe for *backend*'s cold list; True when one started.

    Only the configured harness is probed, and only on a backend that serves
    background work from the shared runtime: that is where a throwaway session
    exists. A picker asking about another harness (a Bot's own ``?backend=``)
    keeps today's answer. A running probe, or one started within
    :data:`COLD_PROBE_COOLDOWN_SECS` for the same backend, starts nothing.
    Never raises.
    """
    try:
        sessions = state.sessions
        if sessions is None or sessions._configured_bg_backend() != backend:
            return False
        if not sessions._bg_backend_supports_runtime():
            return False
        running = getattr(state, _TASK_ATTR, None)
        if running is not None and not running.done():
            return False
        started: dict[str, float] = getattr(state, _STARTED_ATTR, None) or {}
        now = _now()
        last = started.get(backend)
        if last is not None and now - last < COLD_PROBE_COOLDOWN_SECS:
            return False
        started[backend] = now
        setattr(state, _STARTED_ATTR, started)
        task = asyncio.get_running_loop().create_task(_probe(state))
        setattr(state, _TASK_ATTR, task)
        return True
    except Exception:  # noqa: BLE001 - a probe that cannot start leaves the cold answer
        logger.debug("cold model probe did not start", exc_info=True)
        return False


def probe_if_cold(request: Any, backend: str, advertised: object) -> None:
    """The advertised-list read's hook: start a probe when nothing was advertised."""
    if not advertised:
        start_cold_model_probe(request.app.get("state"), backend)


async def _probe(state: Any) -> None:
    """Open and destroy one background session, then tell the dashboards to read again."""
    try:
        handle = await state.sessions.get_bg_session()
    except Exception:  # noqa: BLE001 - the next cold read after the cooldown tries again
        logger.debug("cold model probe: no background session", exc_info=True)
        return
    # session/new has already recorded the list; the handle has no other use.
    try:
        await handle.destroy()
    except Exception:  # noqa: BLE001 - the list is recorded either way
        logger.debug("cold model probe: background handle did not close", exc_info=True)
    state.broadcast_ws(MODELS_CHANGED_EVENT, {})
