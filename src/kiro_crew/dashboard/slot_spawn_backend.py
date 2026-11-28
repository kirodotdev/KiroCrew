"""Which backend a chat slot's next session launches on."""

from __future__ import annotations

import asyncio
from typing import Any


async def slot_spawn_backend(slot: Any) -> str:
    """The runtime backend *slot*'s next session launches on.

    Its own pick (``slot.backend``, the stored spelling) through the selection
    gate, else the configured ``agent.acp_backend`` (already normalized at load).
    Read by the folder pre-flights, which judge a workspace by the backend that
    will run it, as the spawn-time guard does.
    """
    from kiro_crew.agent_sdk.backends import backend_from_stored, resolve_selected_backend
    from kiro_crew.config import KiroCrewConfig

    picked = backend_from_stored(getattr(slot, "backend", "") or None)
    if isinstance(picked, str):
        return resolve_selected_backend(picked)
    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    return cfg.agent.acp_backend
