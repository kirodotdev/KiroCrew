"""Keep a harness's own memory off while a provider replaces Crew's memory.

The Kiro agent harness can run a memory of its own. Crew's ``initialize`` names no
setting for it, so without a pin the harness decides on its own whether it runs.
A provider in ``replace`` mode is the one memory the owner chose, so a spawn made
while ``memory.provider.mode`` is ``replace`` sends ``userMemoryOptIn: {"enabled":
false}`` in ``_meta.kiro.settings``.

Only a harness that reads that channel gets it: the caller sends it inside the
``client_meta_settings`` branch of the spawn handshake, which is a positive
capability of the harness, never the absence of another one. In ``builtin`` and
``augment`` mode nothing is sent and the harness keeps deciding as before.

shortcut: judged once per spawn from config, because ``initialize`` is per process
and a pooled process hosts many sessions. A process spawned before a switch keeps
what it was sent until it restarts; upgrade to a per-session setting if the harness
gains one.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("kiro_crew.memory_provider")

#: The ``_meta.kiro.settings`` key the harness reads its own memory switch from.
USER_MEMORY_SETTING = "userMemoryOptIn"


def harness_memory_settings(config: Any = None) -> dict[str, Any]:
    """The settings a spawn adds now: the pin in ``replace`` mode, else nothing."""
    from kiro_crew.memory_provider import resolver

    try:
        cfg = resolver._load_config(config)
        mode = cfg.memory.provider.mode
    except Exception:  # noqa: BLE001 - an unreadable config pins nothing, as before
        logger.debug("memory provider mode unreadable at spawn", exc_info=True)
        return {}
    if mode != resolver.MODE_REPLACE:
        return {}
    return {USER_MEMORY_SETTING: {"enabled": False}}
