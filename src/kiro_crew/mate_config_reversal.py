"""Undo the crewmate migration when Crewmate 2.0 is turned off.

:func:`kiro_crew.mate_config_migration.migrate_existing_crewmates` moves each
crewmate onto its own file and its conductor, and records what it moved in
``migrated.json`` (beside ``~/.kiro/mates/agents/``): ``migrated`` names the
crewmates, ``threads[<name>]`` the agent each ran on before (``from``) and the
DM threads it repointed (``repointed``). A crewmate created while the switch was
on is recorded the same way by ``mate_config.adopt_own_file``.

With the switch off (:mod:`kiro_crew.crewmate_v2`) the startup runs
:func:`reverse_migration` instead, which puts back what the migration changed:

* each recorded crewmate whose ``kiro_agent`` is still its own id goes back to
  its ``from`` agent; one with no ``from`` (it was bound to the conductor, as the
  first crewmate of a fresh install is) goes back to the default template;
* any other crewmate still bound to ``kirocrew-mate-conductor`` goes to the
  default template as well, since the conductor is nothing a chat runs on while
  the switch is off;
* each repointed thread record whose ``template_id`` is still the conductor goes
  back to ``from``, through the same compare-and-set write the migration used
  (``bind_session_execution``, ``vouch=False``), so a record that moved meanwhile
  is left alone;
* the crewmate's file stays on disk (worker tools added on cards are kept), and
  it is taken out of ``migrated`` so turning the switch on again migrates it
  afresh. What was reversed is kept under ``reversed`` for the record.

Idempotent: a second boot finds nothing recorded and changes nothing. One log
line per crewmate. Never raises; a crewmate it cannot restore is logged and left
recorded, so the next boot tries again.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any

from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ReversalReport:
    #: Crewmate -> the agent its binding now names.
    restored: dict[str, str] = dataclasses.field(default_factory=dict)
    #: Crewmate -> the thread keys put back on that agent.
    threads: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    failed: dict[str, str] = dataclasses.field(default_factory=dict)


def _default_template() -> str:
    from pathlib import Path

    from kiro_crew.agent_files import AGENT_FILENAME

    return Path(AGENT_FILENAME).stem


def _restore_binding(name: str, target: str) -> None:
    """Point *name*'s ``kiro_agent`` at *target* under the config lock (the create write)."""
    from kiro_crew.memory_stores import persist_member_config

    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(name)
    if binding is None:
        return
    prior_store = binding.memory_store
    binding.kiro_agent = target
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})


def _restore_threads(keys: list[str], member_id: str, source: str) -> tuple[list[str], bool]:
    """Put each repointed record in *keys* back on *source*; ``(moved, complete)``."""
    from kiro_crew.execution_context import bind_session_execution, read_session_execution
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    moved: list[str] = []
    complete = True
    for key in keys:
        try:
            current = read_session_execution(key)
        except Exception:  # noqa: BLE001 - an unreadable record stays as it is
            logger.warning("crewmate reversal: thread %s could not be read", key, exc_info=True)
            complete = False
            continue
        if (
            current is None
            or current.member_id != member_id
            or current.selection_kind != "member"
            or current.template_id != MATE_CONDUCTOR_AGENT
        ):
            continue
        back = dataclasses.replace(current, template_id=source)
        try:
            bind_session_execution(key, back, replace_existing=True, expected=current, vouch=False)
        except Exception:  # noqa: BLE001 - a record that moved meanwhile is left
            logger.warning("crewmate reversal: thread %s was not put back", key, exc_info=True)
            complete = False
            continue
        moved.append(key)
    return moved, complete


def reverse_migration(cfg: KiroCrewConfig | None = None) -> ReversalReport:
    """Undo every recorded move (module docstring). Never raises."""
    from kiro_crew import mate_config_migration as mig
    from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT

    report = ReversalReport()
    try:
        cfg = cfg or KiroCrewConfig.load()
        data = mig._read_ledger()
    except Exception:
        logger.warning("crewmate reversal: could not read the crewmates", exc_info=True)
        return report
    names = mig._names(data)
    raw_threads = data.get("threads")
    threads: dict[str, Any] = raw_threads if isinstance(raw_threads, dict) else {}
    default = _default_template()
    # Every crewmate the ledger names, then any other still on the conductor.
    todo: dict[str, str] = {}
    for name in sorted(names | set(threads)):
        recorded = threads.get(name)
        source = recorded.get("from") if isinstance(recorded, dict) else ""
        source = source if isinstance(source, str) else ""
        todo[name] = source if source and source != MATE_CONDUCTOR_AGENT else default
    for name, bound in cfg.agents.items():
        if name not in todo and getattr(bound, "kiro_agent", "") == MATE_CONDUCTOR_AGENT:
            todo[name] = default
    if not todo:
        return report
    raw_reversed = data.get("reversed")
    reversed_log: dict[str, Any] = raw_reversed if isinstance(raw_reversed, dict) else {}
    for name, target in todo.items():
        binding = cfg.agents.get(name)
        label = mig._label(cfg, name)
        recorded = threads.get(name)
        entry: dict[str, Any] = recorded if isinstance(recorded, dict) else {}
        try:
            if binding is not None and binding.kiro_agent in (name, MATE_CONDUCTOR_AGENT):
                _restore_binding(name, target)
            member_id = str(getattr(binding, "member_id", "") or "").strip()
            moved: list[str] = []
            complete = True
            if member_id and entry:
                moved, complete = _restore_threads(
                    mig._strings(entry.get("repointed")), member_id, target
                )
        except Exception as exc:  # noqa: BLE001 - left recorded, tried again next boot
            report.failed[name] = str(exc)
            logger.warning("crewmate reversal: %s was not moved back: %s", label, exc)
            continue
        if not complete:
            report.failed[name] = "a thread could not be put back"
            logger.warning("crewmate reversal: %s keeps a thread on its conductor", label)
            continue
        report.restored[name] = target
        report.threads[name] = moved
        names.discard(name)
        threads.pop(name, None)
        reversed_log[name] = {"to": target, "threads": moved, "at": int(time.time())}
        if binding is not None:
            logger.info(
                "crewmate reversal: %s is back on %r with %d earlier thread(s); "
                "its own file is kept",
                label,
                target,
                len(moved),
            )
    data["migrated"] = sorted(names)
    data["threads"] = threads
    data["reversed"] = reversed_log
    try:
        mig._write_ledger(data)
    except OSError:
        logger.warning("crewmate reversal: migrated.json not updated", exc_info=True)
    return report


def migrate_or_reverse(cfg: KiroCrewConfig, on: bool, migrate: Any = None) -> Any:
    """The startup step: the migration while Crewmate 2.0 is *on*, else its reversal.

    *migrate* is the migration callable the dashboard imported (so a test that
    patches it there is honoured); ``None`` takes it from its module.
    """
    if on:
        if migrate is None:
            from kiro_crew.mate_config_migration import migrate_existing_crewmates as migrate
        return migrate(cfg)
    return reverse_migration(cfg)


__all__ = ["ReversalReport", "migrate_or_reverse", "reverse_migration"]
