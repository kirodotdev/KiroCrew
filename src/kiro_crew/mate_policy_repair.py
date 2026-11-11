"""Startup repair: give a moved Bot back the restrictions its old agent set.

An earlier startup migration (``mate_config_migration``) moved a Bot onto its
own file without the ``hooks``, ``permissions`` and ``toolsSettings`` of the
agent it ran on, and its notice only named them. Its worker then ran without
the old agent's deny rules, so an action that agent denied was asked, and an
``auto`` approval mode approved it.

Per moved Bot still bound to its own file, the old agent the ledger names
(``threads.<name>.from``, read as :mod:`kiro_crew.mate_mount_repair` reads it)
is compared with what the worker merged from the file keeps
(``mate_config_migration.dropped_restrictions``). When a restriction is missing:

- the old agent's restrictions are layered into the file
  (``mate_config_migration.carried_policy``, ``mate_spec_policy.layer_policy``)
  when the worker then keeps every one of them, gains nothing, and runs on a
  backend that applies them; else
- the Bot is pointed back at its old agent, which applies them whole, when that
  agent is still readable at the user level (compare-and-set, as the move's
  repoint); else
- it is logged and left unchecked, so a later start tries again: nothing safer
  can be written.

Each repaired Bot owes its user one notice in its chat
(``mate_upgrade_notice.mark_policy_owed``). Each Bot is checked once:
``migrated.json`` lists it under ``policy_checked``. Never raises.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kiro_crew.agent_files import is_mate_service_agent
from kiro_crew.agent_sdk import agent_specs
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mate_config import MateConfigError, source_agents_dir, write_mate_spec
from kiro_crew.mate_spec_policy import SpecPolicyUncarried, layer_policy
from kiro_crew.mate_worker import merge_worker_spec, worker_additions
from kiro_crew.user_json import copy_json_document

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


def restored_policy(
    cfg: KiroCrewConfig,
    base: dict[str, Any],
    name: str,
    old: dict[str, Any],
    mate_file: dict[str, Any],
) -> dict[str, Any] | None:
    """*mate_file* with *old*'s restrictions layered in, ``None`` when that cannot be done exactly.

    Exactly: the worker merged from the result keeps every restriction *old*
    states, gains no grant *mate_file* did not already give it, and runs on a
    backend that applies them. *mate_file* is not changed.
    """
    from kiro_crew.mate_config_migration import (
        _policy_backend_refusal,
        carried_policy,
        differences,
        dropped_restrictions,
    )

    out = copy_json_document(mate_file)
    try:
        layer_policy(out, carried_policy(base, old))
        merged = merge_worker_spec(base, worker_additions(base, out))
    except SpecPolicyUncarried:
        return None
    if dropped_restrictions(base, old, merged):
        return None
    if differences(base, old, out).gained - differences(base, old, mate_file).gained:
        return None
    if _policy_backend_refusal(cfg, name, out):
        return None
    return out


def _point_back(name: str, source: str) -> bool:
    """Point Bot *name* back at *source* when *source* is readable at the user level."""
    from kiro_crew.memory_stores import persist_member_config

    try:
        agent_specs.load_agent_spec(source_agents_dir(source), source)
    except (agent_specs.AgentSpecUnreadable, OSError):
        return False
    fresh = KiroCrewConfig.load()
    binding = fresh.agents.get(name)
    if binding is None or binding.kiro_agent != name:
        return False
    store = binding.memory_store
    binding.kiro_agent = source
    persist_member_config(
        fresh,
        name,
        expected_store=store,
        expected_values={"kiro_agent": name},
        changed_fields={"kiro_agent"},
    )
    return True


def _repair_one(cfg: KiroCrewConfig, base: dict[str, Any], name: str, source: str) -> bool | None:
    """Repair Bot *name* from its old agent *source*: whether it changed.

    ``None`` when its file or its old agent cannot be read now, so a later start
    checks it again.
    """
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
    rules = "; ".join(missing)
    fixed = restored_policy(cfg, base, name, old, mate_file)
    if fixed is not None:
        try:
            write_mate_spec(name, fixed)
        except MateConfigError:
            logger.warning("crewmate policy repair: %s's file was not written", name)
            return None
        mark_policy_owed(name, f"these limits apply to its worker again: {rules}")
        logger.warning("crewmate policy repair: %s's worker keeps %s again", name, rules)
        return True
    if _point_back(name, source):
        mark_policy_owed(
            name,
            f"its worker could not keep them ({rules}), so it runs on the agent it "
            f"ran on before the upgrade ({source}) again, which keeps them",
        )
        logger.warning(
            "crewmate policy repair: %s points back at %r to keep %s", name, source, rules
        )
        return True
    # Left unchecked, so every start tries again once the old agent is readable.
    logger.warning(
        "crewmate policy repair: %s's worker does not keep %s and %r cannot be read to "
        "point it back",
        name,
        rules,
        source,
    )
    return None


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
