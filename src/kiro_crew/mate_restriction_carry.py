"""Carrying a moved Bot's old-agent restrictions into its file, or holding them.

The startup move (``mate_config_migration``) and the repair of a Bot an earlier
release moved (``mate_policy_repair``) both call :func:`carry_restrictions`: each
restriction the worker keeps exactly is written into the Bot's file, and the rest
are held for a person (``mate_held_rules.split_policy``). The repair records
them first (:func:`_hold`); the move records them just before its repoint. Imported back into ``mate_config_migration``, which callers use.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger("kiro_crew.mate_config_migration")


def _hold(name: str, held: list[dict[str, str]]) -> bool:
    """Record *held* for crewmate *name* before its file changes; whether it was recorded.

    Refused while another instance keeps a Bot file of that name in this Kiro
    home (``mate_config.refuse_foreign_file``): the entry is that Bot's.
    """
    from kiro_crew.mate_config import MateConfigError, refuse_foreign_file
    from kiro_crew.mate_config_migration import LedgerUnreadable, _member_id
    from kiro_crew.mate_held_rules import record_held

    try:
        refuse_foreign_file(name)
        record_held(name, _member_id(name), held)
    except (OSError, LedgerUnreadable, MateConfigError):
        logger.warning("crewmate migration: %s's held limits not recorded", name, exc_info=True)
        return False
    return True


def _drop_hold(name: str, member_id: str | None) -> None:
    """Undo the hold the move recorded for crewmate *name* under *member_id*.

    *name* stays on its old agent, which still enforces its own limits, so the
    hold would only make its calls ask a person, with no notice saying why; the
    next start that moves it holds them again. An entry recorded under another
    identity is left. ``None`` (no hold recorded) does nothing. Never raises.
    """
    if member_id is None:
        return
    from kiro_crew.mate_held_rules import drop_held

    try:
        drop_held(name, member_id)
    except Exception:  # noqa: BLE001 - logged; the hold only asks more, never less
        logger.warning("crewmate migration: %s's held limits not cleared", name, exc_info=True)


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


def worker_on_kiro_cli(cfg: KiroCrewConfig, name: str) -> bool:
    """Whether crewmate *name*'s worker runs on Kiro CLI; ``False`` when that cannot be told."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
    from kiro_crew.mate_worker import crewmate_worker_backend

    try:
        return crewmate_worker_backend(cfg, name) == ACP_BACKEND_KIRO
    except Exception:  # noqa: BLE001 - only the notice's wording reads this
        return False


def carry_restrictions(
    cfg: KiroCrewConfig,
    base: dict[str, Any],
    name: str,
    source: dict[str, Any],
    mate_file: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """*mate_file* with *source*'s restrictions it can carry, and the ones held for a person.

    ``mate_held_rules.split_policy`` over :func:`carried_policy`:
    the worker always starts, and a held restriction's calls are never approved
    without a person.
    """
    from kiro_crew.mate_held_rules import split_policy

    return split_policy(
        applies_restrictions(cfg, name), base, carried_policy(base, source), mate_file
    )


#: ``toolsSettings`` keys that only grant: they let a call run, never refuse one.
_GRANT_ONLY_SETTINGS = frozenset(
    {
        "allowedCommands",
        "allowedPaths",
        "autoAllowReadonly",
        "trusted",
        "trustedAgents",
        "allowReadOnly",
    }
)


def _limiting_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """*settings* without the entries made only of grant keys.

    An entry that also limits (``denyByDefault``, ``deniedPaths``) is kept whole:
    its grants are the exceptions that limit makes, so dropping them would refuse
    calls the old agent allowed.
    """
    return {
        tool: entry
        for tool, entry in settings.items()
        if not (isinstance(entry, dict) and set(entry) <= _GRANT_ONLY_SETTINGS)
    }


def carried_policy(base: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """The ``hooks``, ``permissions`` and ``toolsSettings`` *source* adds to the worker *base*.

    Every restriction is carried, so the worker runs under it as the old agent
    did (``mate_spec_policy.layer_policy``). A ``permissions`` rule that only
    allows is a grant and is left out, as every other grant the move compares
    (``mate_config_migration._restrictions``). So is a ``toolsSettings`` entry made only of
    grant keys (:data:`_GRANT_ONLY_SETTINGS`): it limits nothing.
    """
    from kiro_crew.mate_spec_policy import fold_shell_settings, policy_of, without_held

    policy = policy_of(source)
    if "toolsSettings" in policy:
        policy["toolsSettings"] = fold_shell_settings(policy["toolsSettings"])
    policy = without_held(policy, base)
    settings = policy.get("toolsSettings")
    if isinstance(settings, dict):
        kept_settings = _limiting_settings(settings)
        if kept_settings:
            policy["toolsSettings"] = kept_settings
        else:
            policy.pop("toolsSettings")
    perms = policy.get("permissions")
    if isinstance(perms, dict) and isinstance(perms.get("rules"), list):
        rules = [
            r
            for r in perms["rules"]
            if not (isinstance(r, dict) and r.get("effect") == "allow" and not r.get("exclude"))
        ]
        if rules:
            policy["permissions"] = {**perms, "rules": rules}
        else:
            policy.pop("permissions")
    return policy
