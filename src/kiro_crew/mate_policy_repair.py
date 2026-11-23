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
A Bot held on every call is checked once more (``held_every_call_rechecked``).
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
from kiro_crew.mate_ledger_lock import ledger_lock
from kiro_crew.mate_restriction_carry import _hold
from kiro_crew.mate_spec_policy import SpecPolicyUncarried
from kiro_crew.mate_worker import merge_worker_spec, worker_additions

logger = logging.getLogger(__name__)

#: The ``migrated.json`` key listing every Bot this repair has checked.
CHECKED_KEY = "policy_checked"

#: The ``migrated.json`` key listing every Bot whose every-call hold was read again.
#: An earlier release held a whole ``permissions`` block that named a bundle
#: (``policies``) beside its rules, so one deny in it made every call of that Bot
#: ask, Trust included. Each Bot holding such a limit is checked once more, and
#: keeps only what its rules still need.
RESPLIT_KEY = "held_every_call_rechecked"


#: The ``migrated.json`` key listing every Bot whose file was read once for the bare
#: ``ask`` rules an every-call hold wrote (:func:`_carries_hold_asks`).
ASKS_KEY = "held_asks_rechecked"


def _is_bare_ask(rule: Any) -> bool:
    """Whether *rule* is ``{"capability": c, "effect": "ask"}`` with *c* a held-limit capability."""
    from kiro_crew.mate_held_rules import _ASKABLE

    return (
        isinstance(rule, dict)
        and set(rule) == {"capability", "effect"}
        and rule.get("effect") == "ask"
        and rule.get("capability") in _ASKABLE
    )


def _carries_hold_asks(mate_file: Any) -> bool:
    """Whether *mate_file* still holds what an every-call hold wrote: a bare ask on every capability.

    That hold wrote one for each capability (``mate_held_rules.split_policy``). A file
    whose owner pruned them lacks part of the set, so its asks are the
    owner's choice and stay.
    """
    from kiro_crew.mate_held_rules import _ASKABLE

    perms = mate_file.get("permissions") if isinstance(mate_file, dict) else None
    rules = perms.get("rules") if isinstance(perms, dict) else None
    if not isinstance(rules, list):
        return False
    return set(_ASKABLE) <= {r["capability"] for r in rules if _is_bare_ask(r)}


def _file_carries_hold_asks(name: str) -> bool:
    """:func:`_carries_hold_asks` for Bot *name*'s file; False when it cannot be read."""
    try:
        doc = json.loads((paths_mod.mate_agents_dir() / f"{name}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return _carries_hold_asks(doc)


def _without_hold_asks(mate_file: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """*mate_file* without the bare ``ask`` rules an every-call hold wrote into it.

    An ``ask`` outranks an ``allow``, the user's own ``~/.kiro/settings`` rules
    included, so those asks made every call ask. They are removed only while the
    file carries the whole set the hold wrote (:func:`_carries_hold_asks`); a rule
    the old agent itself states is kept, and the carry writes back each ask still
    needed.
    """
    if not _carries_hold_asks(mate_file):
        return mate_file
    perms = mate_file["permissions"]
    old_perms = old.get("permissions")
    old_rules = old_perms.get("rules") if isinstance(old_perms, dict) else None
    own = old_rules if isinstance(old_rules, list) else []
    rules = perms["rules"]
    kept = [r for r in rules if not (_is_bare_ask(r) and r not in own)]
    out = dict(mate_file)
    if kept or set(perms) - {"rules"}:
        out["permissions"] = {**perms, "rules": kept}
    else:
        out.pop("permissions")
    return out


def _every_call_held(data: dict[str, Any]) -> set[str]:
    """The Bots whose recorded hold covers every call (``mate_held_rules.ANY``)."""
    from kiro_crew.mate_held_rules import ANY, HELD_KEY

    entries = data.get(HELD_KEY)
    return {
        name
        for name, entry in (entries.items() if isinstance(entries, dict) else ())
        if isinstance(entry, dict)
        and any(
            isinstance(r, dict) and r.get("capability") == ANY for r in entry.get("rules") or []
        )
    }


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _kept(base: dict[str, Any], old: dict[str, Any], mate_file: dict[str, Any]) -> list[str]:
    """What *old* restricts that the worker merged from *mate_file* does not keep."""
    from kiro_crew.mate_config_migration import dropped_restrictions
    from kiro_crew.mate_spec_policy import fold_shell_settings

    try:
        merged = merge_worker_spec(base, worker_additions(base, mate_file))
    except SpecPolicyUncarried:
        merged = dict(base)
    if "toolsSettings" in old:
        # Carried the way the move carries it (``carried_policy``), so a `shell`
        # entry folded into `execute_bash` is not read as a limit still missing.
        old = {**old, "toolsSettings": fold_shell_settings(old["toolsSettings"])}
    return dropped_restrictions(base, old, merged)


def mark_checked(name: str) -> None:
    """Record Bot *name* as needing no repair (``policy_checked``)."""
    from kiro_crew.mate_config_migration import _read_ledger, _write_ledger

    with ledger_lock():
        data = _read_ledger()
        data[CHECKED_KEY] = sorted(set(_strings(data.get(CHECKED_KEY))) | {name})
        _write_ledger(data)


def _repair_one(
    cfg: KiroCrewConfig, base: dict[str, Any], name: str, source: str, *, recheck: bool = False
) -> bool | None:
    """Repair Bot *name* from its old agent *source*: whether it changed.

    ``None`` when its file or its old agent cannot be read now, or its held limits
    cannot be recorded, so a later start checks it again. The file is read and
    written under the lock an owner's edit of it takes, so neither undoes the other.
    """
    from kiro_crew.agent import agents_spec_lock
    from kiro_crew.mate_held_rules import asks_for, held_sentence
    from kiro_crew.mate_upgrade_notice import mark_policy_owed

    try:
        with agents_spec_lock(paths_mod.mate_agents_dir()):
            done = _rewrite(cfg, base, name, source, recheck=recheck)
    except OSError:
        logger.warning("crewmate policy repair: %s's file was not repaired", name, exc_info=True)
        return None
    if not isinstance(done, tuple):
        return done
    missing, held = done
    kept = sorted(set(missing) - {h["text"] for h in held})
    parts = [f"these limits apply to its worker again: {'; '.join(kept)}"] if kept else []
    if held:
        from kiro_crew.mate_restriction_carry import worker_on_kiro_cli

        parts.append(held_sentence(held, kiro_cli=worker_on_kiro_cli(cfg, name)))
    mark_policy_owed(name, "; and ".join(parts), held=asks_for(held))
    logger.warning(
        "crewmate policy repair: %s keeps %d limit(s) again, %d held for a person",
        name,
        len(kept),
        len(held),
    )
    return True


def _rewrite(
    cfg: KiroCrewConfig, base: dict[str, Any], name: str, source: str, *, recheck: bool = False
) -> tuple[list[str], list[dict[str, str]]] | bool | None:
    """:func:`_repair_one`'s read and write of the file: its missing and held limits."""
    from kiro_crew.mate_config_migration import _read_ledger, carry_restrictions
    from kiro_crew.mate_held_rules import HELD_KEY, _bundle_beside_rules
    from kiro_crew.mate_mount_repair import _old_agent

    path = paths_mod.mate_agents_dir() / f"{name}.json"
    try:
        mate_file = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    old = _old_agent(source)
    if not isinstance(mate_file, dict) or old is None:
        return None
    missing = _kept(base, old, mate_file)
    if recheck and not _bundle_beside_rules(old.get("permissions")):
        # The every-call hold is for a limit that really cannot be carried: it stays.
        return False
    if not missing and not recheck:
        return False
    # On a recheck the hold is derived again from the old agent, never assumed
    # empty: a limit beside the bundle that cannot be carried is still held.
    # The asks that hold wrote are dropped too, and the carry writes back each one
    # the new hold still needs.
    start = _without_hold_asks(mate_file, old) if recheck else mate_file
    fixed, held = carry_restrictions(cfg, base, name, old, start)
    # Keep every recorded hold until the file carries its limits.
    if recheck:
        with ledger_lock():
            entries = _read_ledger().get(HELD_KEY) or {}
            recorded = entries.get(name, {}).get("rules") or []
            combined = recorded + [rule for rule in held if rule not in recorded]
            if combined and not _hold(name, combined):
                return None
    elif held and not _hold(name, held):
        return None
    if fixed != mate_file:
        try:
            write_mate_spec(name, fixed)
        except MateConfigError:
            logger.warning("crewmate policy repair: %s's file was not written", name)
            return None
    # The file carries each unheld limit, so its hold can be dropped safely.
    if recheck and not _hold(name, held):
        return None
    if not missing:
        return False
    return missing, held


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
        rechecked = set(_strings(data.get(RESPLIT_KEY)))
        # A Bot held on every call is read once more (``RESPLIT_KEY``), even when checked.
        # Only a checked Bot: an unchecked one still owes its first repair, which
        # writes every carriable deny into its file before anything is marked done.
        asks_done = set(_strings(data.get(ASKS_KEY)))
        # A Bot an every-call recheck already passed, whose file still carries the
        # hold's asks, is read once more: that recheck dropped the hold and kept them.
        # Only such a Bot: the asks of one never held on every call are its owner's.
        asks_due = {n for n in (rechecked - asks_done) & done if _file_carries_hold_asks(n)}
        recheck = ((_every_call_held(data) - rechecked) | asks_due) & done
        owed = {
            name: entry["from"]
            for name, entry in (threads.items() if isinstance(threads, dict) else ())
            if (name not in done or name in recheck)
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
            outcome = _repair_one(cfg, base, name, source, recheck=name in recheck)
        except Exception:  # noqa: BLE001 - one Bot's file never stops the others
            logger.warning("crewmate policy repair: %s not checked", name, exc_info=True)
            continue
        if outcome is None:
            continue
        if outcome:
            repaired.append(name)
        done.add(name)
        if name in recheck:
            rechecked.add(name)
            asks_done.add(name)
    if (
        not done - set(_strings(data.get(CHECKED_KEY)))
        and not rechecked - set(_strings(data.get(RESPLIT_KEY)))
        and not asks_done - set(_strings(data.get(ASKS_KEY)))
    ):
        return repaired
    try:
        with ledger_lock():
            data = _read_ledger()
            data[CHECKED_KEY] = sorted(done | set(_strings(data.get(CHECKED_KEY))))
            data[RESPLIT_KEY] = sorted(rechecked | set(_strings(data.get(RESPLIT_KEY))))
            data[ASKS_KEY] = sorted(asks_done | set(_strings(data.get(ASKS_KEY))))
            _write_ledger(data)
    except Exception:  # noqa: BLE001 - a repaired file keeps its rules; a re-check is a no-op
        logger.warning("crewmate policy repair: not recorded in the ledger", exc_info=True)
    return repaired
