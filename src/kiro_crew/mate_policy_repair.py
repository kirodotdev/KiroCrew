"""Startup repair: give a moved Bot back the restrictions its old agent set.

An earlier startup migration (``mate_config_migration``) moved a Bot onto its
own file without the ``hooks``, ``permissions`` and ``toolsSettings`` of the
agent it ran on, and its notice only named them. Its worker then ran without
the old agent's deny rules, so an action that agent denied was asked, and an
``auto`` approval mode approved it.

Per moved Bot still bound to its own file, the old agent the ledger names
(``threads.<name>.from``, read as :mod:`kiro_crew.mate_mount_repair` reads it)
is compared with what the worker merged from the file keeps
(``mate_config_migration.dropped_restrictions``). When a restriction is missing,
the move's own carry runs (``mate_config_migration.carry_restrictions``): each
one the worker can keep exactly is written into the file, and each other one is
held for a person (``mate_held_rules``): the worker still starts, and no gate
approves a call it covers without a person. Its user is told once, in its chat
and, when a limit is held, on the bell (``mate_upgrade_notice.mark_policy_owed``).
Each Bot is checked once: ``migrated.json`` lists it under ``policy_checked``.
Never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.agent_files import is_mate_service_agent
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mate_config import MateConfigError, write_mate_spec
from kiro_crew.mate_spec_policy import SpecPolicyUncarried
from kiro_crew.mate_worker import merge_worker_spec, worker_additions

logger = logging.getLogger(__name__)

#: The ``migrated.json`` key listing every Bot this repair has checked.
CHECKED_KEY = "policy_checked"


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _kept(base: dict[str, Any], old: dict[str, Any], mate_file: dict[str, Any]) -> list[str]:
    """What *old* restricts that the worker merged from *mate_file* does not keep."""
    from kiro_crew.mate_config_migration import dropped_restrictions

    try:
        merged = merge_worker_spec(base, worker_additions(base, mate_file))
    except SpecPolicyUncarried:
        merged = dict(base)
    return dropped_restrictions(base, old, merged)


def mark_checked(name: str) -> None:
    """Record Bot *name* as needing no repair (``policy_checked``)."""
    from kiro_crew.mate_config_migration import _read_ledger, _write_ledger

    data = _read_ledger()
    data[CHECKED_KEY] = sorted(set(_strings(data.get(CHECKED_KEY))) | {name})
    _write_ledger(data)


def _repair_one(cfg: KiroCrewConfig, base: dict[str, Any], name: str, source: str) -> bool | None:
    """Repair Bot *name* from its old agent *source*: whether it changed.

    ``None`` when its file or its old agent cannot be read now, or its held limits
    cannot be recorded, so a later start checks it again.
    """
    from kiro_crew.mate_config_migration import _hold, carry_restrictions
    from kiro_crew.mate_held_rules import held_sentence
    from kiro_crew.mate_mount_repair import _old_agent
    from kiro_crew.mate_upgrade_notice import mark_policy_owed

    path = paths_mod.mate_agents_dir() / f"{name}.json"
    try:
        mate_file = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    old = _old_agent(source)
    if not isinstance(mate_file, dict) or old is None:
        return None
    missing = _kept(base, old, mate_file)
    if not missing:
        return False
    fixed, held = carry_restrictions(cfg, base, name, old, mate_file)
    # Recorded before the file changes, so no start runs the worker unguarded.
    if held and not _hold(name, held):
        return None
    if fixed != mate_file:
        try:
            write_mate_spec(name, fixed)
        except MateConfigError:
            logger.warning("crewmate policy repair: %s's file was not written", name)
            return None
    kept = sorted(set(missing) - {h["text"] for h in held})
    parts = [f"these limits apply to its worker again: {'; '.join(kept)}"] if kept else []
    if held:
        parts.append(held_sentence(held))
    mark_policy_owed(name, "; and ".join(parts), held=bool(held))
    logger.warning(
        "crewmate policy repair: %s keeps %d limit(s) again, %d held for a person",
        name,
        len(kept),
        len(held),
    )
    return True


def restore_dropped_policy(cfg: KiroCrewConfig | None = None) -> list[str]:
    """Run the repair over every moved Bot not checked yet; the Bots repaired. Never raises."""
    from kiro_crew.mate_config_migration import (
        _already_moved,
        _read_ledger,
        _worker_base,
        _write_ledger,
    )

    repaired: list[str] = []
    try:
        data = _read_ledger()
        threads = data.get("threads")
        done = set(_strings(data.get(CHECKED_KEY)))
        owed = {
            name: entry["from"]
            for name, entry in (threads.items() if isinstance(threads, dict) else ())
            if name not in done
            and isinstance(entry, dict)
            and isinstance(entry.get("from"), str)
            and entry["from"]
            and not is_mate_service_agent(name)
        }
        if not owed:
            return repaired
        cfg = cfg or KiroCrewConfig.load()
        base = _worker_base()
    except Exception:  # noqa: BLE001 - best effort; the next start tries again
        logger.warning("crewmate policy repair: ledger or worker base unreadable", exc_info=True)
        return repaired
    for name, source in sorted(owed.items()):
        binding = cfg.agents.get(name)
        member_id = str(getattr(binding, "member_id", "") or "")
        if binding is None or binding.kiro_agent != name:
            continue
        if not _already_moved(data, name, member_id):
            continue
        try:
            outcome = _repair_one(cfg, base, name, source)
        except Exception:  # noqa: BLE001 - one Bot's file never stops the others
            logger.warning("crewmate policy repair: %s not checked", name, exc_info=True)
            continue
        if outcome is None:
            continue
        if outcome:
            repaired.append(name)
        done.add(name)
    if not done - set(_strings(data.get(CHECKED_KEY))):
        return repaired
    try:
        data = _read_ledger()
        data[CHECKED_KEY] = sorted(done | set(_strings(data.get(CHECKED_KEY))))
        _write_ledger(data)
    except Exception:  # noqa: BLE001 - a repaired file keeps its rules; a re-check is a no-op
        logger.warning("crewmate policy repair: not recorded in the ledger", exc_info=True)
    return repaired
