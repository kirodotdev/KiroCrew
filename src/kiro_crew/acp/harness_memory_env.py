"""``agent.harness_memory``: whether a KAS engine process starts with Kiro Memory on.

The engine gates its memory on two rollout experiments and lets
``KIRO_FEATURE_MEMORY_EXTERNAL_ENABLED`` override the external one in both
directions. The engine Crew pins defaults both experiments off; newer engines
default them on. Crew sets the variable from the setting so a KAS chat gets the
same answer on either engine and on either route (Crew's own bundle, or the
kiro-cli relay, whose KAS child inherits the relay's environment).

A value already in the environment the child inherits, the gateway's own or a
per-session overlay, wins: an operator's explicit choice is never overridden.
When config cannot be read, nothing is set and the engine keeps its own default.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

__all__ = ["KAS_MEMORY_ENV", "apply_harness_memory_env"]

#: The engine's env override for the ``KAS_MEMORY_EXTERNAL`` experiment.
KAS_MEMORY_ENV = "KIRO_FEATURE_MEMORY_EXTERNAL_ENABLED"


def _configured() -> bool | None:
    """``agent.harness_memory`` as configured, or None when config cannot be read.

    ``KiroCrewConfig.load`` does file IO on a cache miss: call it off the loop.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        return KiroCrewConfig.load().agent.harness_memory is True
    except Exception:  # config must never break a spawn
        logger.debug(
            "harness_memory: config unavailable, leaving the engine default", exc_info=True
        )
        return None


def apply_harness_memory_env(env: MutableMapping[str, str]) -> None:
    """Set :data:`KAS_MEMORY_ENV` in *env* from ``agent.harness_memory`` unless already present."""
    if KAS_MEMORY_ENV in env:
        return
    enabled = _configured()
    if enabled is not None:
        # The engine accepts exactly "true" and "false"; anything else is ignored.
        env[KAS_MEMORY_ENV] = "true" if enabled else "false"
