"""``agent.child_env_defaults``: operator env defaults for spawned kiro-cli children.

One helper, called from both places a kiro-cli child environment is built: the
kiro-family harness hook (``KiroHarness`` / ``KasHarness.apply_spawn_env``, which
``AcpRuntime`` runs for session-serving children, subagents included) and the
auxiliary ``AcpClient`` spawn (``acp.client._resolve_spawn_env`` with
``kiro_api_key=True``). Both call sites run it off the event loop, before
the agent-env scrub, so a default can never reintroduce a name the scrub denies.

A default only fills a gap: a key already present in the environment the child
inherits -- the gateway's own, or a per-session overlay -- is left as it is,
including an explicitly empty value.
"""

from __future__ import annotations

import logging
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

__all__ = ["apply_child_env_defaults"]


def _configured_defaults() -> dict[str, str]:
    """The validated mapping from config; empty when config is unreadable.

    ``KiroCrewConfig.load`` is fingerprint-cached, so a spawn re-reads nothing
    unless config.json changed and the read itself is cheap on a cache hit. The
    agent section is still rebuilt on every load, so an invalid entry is warned
    about (``sections.coerce_child_env_defaults``) on each spawn for as long as
    it stays invalid. It does file IO on a cache miss, so callers run this off
    the loop.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        return dict(KiroCrewConfig.load().agent.child_env_defaults)
    except Exception:  # config must never break a spawn
        logger.debug("child_env_defaults: config unavailable, applying none", exc_info=True)
        return {}


def apply_child_env_defaults(env: MutableMapping[str, str]) -> None:
    """Set each configured default in *env* whose key is not already present.

    The mapping is read from config on every call. Mutates *env* in place.
    """
    for key, value in _configured_defaults().items():
        if key not in env:
            env[key] = value
