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

Each of ``TOKIO_WORKER_THREADS`` and ``RAYON_NUM_THREADS`` that config leaves
out gets the host-sized default from
:func:`kiro_crew.host_task_sizing.thread_pool_env_defaults`; the merge is per
name, so configuring one still leaves the other at the host default. kiro-cli
otherwise starts one worker per core in each pool, and every thread counts
against the agents slice's ``TasksMax``. That default exists only where the
slice's task ceiling is in force; on any other host it is empty and only the
configured map applies.
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


def _host_defaults() -> dict[str, str]:
    """The host-sized thread-pool defaults; empty when the host cannot be read."""
    try:
        from kiro_crew.host_task_sizing import thread_pool_env_defaults

        return thread_pool_env_defaults()
    except Exception:  # sizing must never break a spawn
        logger.debug("child_env_defaults: host sizing unavailable", exc_info=True)
        return {}


def apply_child_env_defaults(env: MutableMapping[str, str]) -> None:
    """Set each default in *env* whose key is not already present.

    The defaults are the host-sized ones overlaid by the configured map, so a
    configured value wins per key. The mapping is read from config on every
    call. Mutates *env* in place.
    """
    for key, value in {**_host_defaults(), **_configured_defaults()}.items():
        if key not in env:
            env[key] = value
