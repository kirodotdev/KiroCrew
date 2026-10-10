"""``agent.env``: operator environment variables for every agent child.

Read once per spawn by the shared launch tail (:func:`kiro_crew.acp.launch.launch`),
which both drivers and every backend go through, and laid over the inherited
gateway environment BEFORE the driver's own variables and the agent environment
scrub. So the precedence is: the gateway's environment, then this map, then
everything Kiro Crew sets for the session itself; and the scrub still removes a
denied name whatever put it there.

Config load already drops names the config layer refuses
(``sections.coerce_agent_env``). This module adds the one check that needs the
sandbox's own list: a name the agent environment scrub strips would be removed
after it is applied, so it is dropped here with a warning instead of vanishing
without one.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

__all__ = ["agent_env_overlay"]


def agent_env_overlay() -> dict[str, str]:
    """The ``agent.env`` variables to lay over an agent child's environment.

    Empty when config is unreadable: config must never break a spawn. Does file
    IO on a config cache miss, so callers run it off the event loop. The warning
    for a dropped name carries the key only, never the value.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        configured = dict(KiroCrewConfig.load().agent.env)
    except Exception:
        logger.debug("agent.env: config unavailable, applying none", exc_info=True)
        return {}
    if not configured:
        return {}
    from kiro_crew.sandbox import agent_env_scrub_prefixes

    scrubbed = agent_env_scrub_prefixes()
    out: dict[str, str] = {}
    for key, value in configured.items():
        # Windows environment names are case-insensitive: fold to the spelling
        # ``os.environ`` holds there, so the map replaces an inherited value
        # instead of adding a second entry for the same variable. The scrub test
        # is upper-cased everywhere, so a lower-case spelling of a scrubbed name
        # is refused rather than slipping past a case-sensitive prefix match.
        if os.name == "nt":
            key = key.upper()
        if any(key.upper().startswith(prefix) for prefix in scrubbed):
            logger.warning(
                "agent.env: ignoring %r, the agent environment scrub removes that name", key
            )
            continue
        out[key] = value
    return out
