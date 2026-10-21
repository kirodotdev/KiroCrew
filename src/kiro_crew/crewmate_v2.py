"""The Crewmate 2.0 switch: ``dashboard.crewmate_v2``, on by default.

Crewmate 2.0 is the conductor/worker split: a crewmate owns its file under
``~/.kiro/mates/agents/``, its chat runs on ``kirocrew-mate-conductor``, every
task goes to its own ``kirocrew-mate-worker`` session, and the startup migration
moves existing crewmates onto that shape. With the switch off the gateway does
what it did before any of that: no migration, no crewmate file on create, the
chat on the bound template, no worker redirect, and none of the conductor-only
prompt pieces. A gateway that ran with it on reverses the migration at its next
start (:func:`kiro_crew.mate_config_migration.reverse_migration`).

Read ONCE per process (:func:`enabled` latches the first answer), so it takes
effect on the next restart, like every other ``restart=True`` setting: a crewmate
must never be half on the conductor and half on its template within one run. The
Settings toggle sits under the Crew Members preview card
(``FeaturePreviewsSection.tsx``).

A config that cannot be read answers the default (on) and is not latched, so a
later read can still find the setting. Answering off there would reverse the
migration at a start whose config read failed, which is not something the user asked for.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The config path of the switch (``config/sections_dashboard.py``).
CONFIG_KEY = "dashboard.crewmate_v2"

#: What the switch answers with no key set, and when the config cannot be read.
DEFAULT = True

_latched: bool | None = None


def _read() -> bool | None:
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        return bool(KiroCrewConfig.load().dashboard.crewmate_v2)
    except Exception:  # noqa: BLE001 - an unreadable config is the default, not latched
        logger.debug(
            "crewmate 2.0: config unreadable, reading the switch as its default", exc_info=True
        )
        return None


def enabled() -> bool:
    """Whether Crewmate 2.0 is on in this process (latched on the first good read)."""
    global _latched
    if _latched is None:
        value = _read()
        if value is None:
            return DEFAULT
        _latched = value
    return _latched


def latch() -> bool:
    """Read the switch now and hold it for the rest of the process. Startup calls this."""
    global _latched
    _latched = None
    return enabled()
