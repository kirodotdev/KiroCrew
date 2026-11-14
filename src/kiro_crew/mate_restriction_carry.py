"""Carrying a moved Bot's old-agent restrictions into its file, or holding them.

The startup move (``mate_config_migration``) and the repair of a Bot an earlier
release moved (``mate_policy_repair``) both call :func:`carry_restrictions`: each
restriction the worker keeps exactly is written into the Bot's file, and the rest
are held for a person (``mate_held_rules.split_policy``), recorded first by
:func:`_hold`. Imported back into ``mate_config_migration``, which callers use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger("kiro_crew.mate_config_migration")


def _hold(name: str, held: list[dict[str, str]]) -> bool:
    """Record *held* for crewmate *name* before anything moves; whether it was recorded."""
    from kiro_crew.mate_config_migration import LedgerUnreadable, _member_id
    from kiro_crew.mate_held_rules import record_held

    try:
        record_held(name, _member_id(name), held)
    except (OSError, LedgerUnreadable):
        logger.warning("crewmate migration: %s's held limits not recorded", name, exc_info=True)
        return False
    return True


def applies_restrictions(cfg: KiroCrewConfig, name: str) -> bool:
    """Whether crewmate *name*'s worker backend runs a file's restrictions (KAS only).

    One that cannot be resolved counts as not running them, so nothing is written
    that would stop its worker; every restriction is then held.
    """
    from kiro_crew.mate_spec_policy import applies_spec_policy
    from kiro_crew.mate_worker import crewmate_worker_backend

    try:
        return applies_spec_policy(crewmate_worker_backend(cfg, name))
    except Exception:  # noqa: BLE001 - unknown backend: hold, never refuse to start
        return False


def carry_restrictions(
    cfg: KiroCrewConfig,
    base: dict[str, Any],
    name: str,
    source: dict[str, Any],
    mate_file: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """*mate_file* with *source*'s restrictions it can carry, and the ones held for a person.

    ``mate_held_rules.split_policy`` over ``mate_config_migration.carried_policy``:
    the worker always starts, and a held restriction's calls are never approved
    without a person.
    """
    from kiro_crew.mate_config_migration import carried_policy
    from kiro_crew.mate_held_rules import split_policy

    return split_policy(
        applies_restrictions(cfg, name), base, carried_policy(base, source), mate_file
    )
