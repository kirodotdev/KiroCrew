"""One-time startup migration: give every EXISTING crewmate its own owned config.

RFC *mate-owned-agent-config* Phase 2. A crewmate created before this release
still has ``kiro_agent`` pointing at a shared ``~/.kiro/agents/`` template --
exactly the shared-pointer model Phase 1 replaces for NEW crewmates. Phase 1
and Phase 2 ship in the same release deliberately (see the RFC and
kirodotdev/KiroCrew#21381): if existing crewmates kept pointing at shared
templates while new ones owned their config, the product would carry two kinds
of crewmate at once, and an owner editing an old crewmate's Capabilities would
still silently edit the shared template every other crewmate on it depends on.

This module runs once at gateway startup and, for every crewmate whose
``kiro_agent`` does not yet resolve under ``mates/agents/``:

1. copies the resolved template's payload into
   ``~/.kiro/mates/agents/<id>.json`` (:func:`kiro_crew.acp.mate_config.write_mate_config`,
   the exact eager-copy mechanism Phase 1 uses at create);
2. asserts the copy projects to the SAME ``customAgents`` payload the shared
   template does today (ignoring ``name``, which legitimately differs -- that
   difference IS the repoint) -- the dry-run proof the RFC's migration plan
   requires BEFORE any pointer moves;
3. only once every crewmate's copy has been written and proven equivalent,
   repoints each ``kiro_agent`` to its own id, one member at a time, under the
   same optimistic-lock write ``_eager_copy_mate_config`` uses at create.

Design, matching :mod:`kiro_crew.channel_transcript_migration`'s shape (an
additive, idempotent, best-effort pass with no deletion risk) rather than
:mod:`kiro_crew.crewmate_prune_migration`'s (a destructive one that needs a
cross-process lock and a request-blocking gate): this migration never deletes
anything and never removes a shared template, so a crash or a race costs at
worst "runs again next boot", not lost data.

* **Equivalence BEFORE any pointer moves.** Every candidate's copy is written
  and checked FIRST; pointers are repointed only in a second pass, once every
  candidate has passed. A crewmate whose copy does not project identically is
  a migration bug caught before any binding changes -- it is skipped and
  logged, not forced through.
* **Two directories, two helpers, never one shared root.** The user-level
  agents dir and the mate dir are resolved through :func:`kiro_crew.config.paths.kiro_agents_dir`
  and :func:`kiro_crew.config.paths.mate_agents_dir` respectively and NEVER
  assumed to share a parent -- an isolated instance (a pod) can resolve them
  to different roots. Both are asserted to exist (or be creatable) before any
  copy runs.
* **Idempotent.** A crewmate already on its own id (``kiro_agent == name`` and
  an owned file exists) is not a candidate at all. A crash mid-pass leaves
  some crewmates migrated and some not; the next boot picks up exactly the
  ones still pointing at a shared template.
* **Best-effort per crewmate, not all-or-nothing.** One crewmate's copy or
  repoint failing does not block another's: each is attempted independently
  and failures are logged, matching :func:`kiro_crew.dashboard.agent_admin.crew_records._eager_copy_mate_config`'s
  stance that a crewmate already published must never be left broken by this
  pass. Skipped crewmates keep the pre-RFC shared-pointer model, which still
  works, so there is no user-visible breakage either way.
* **Off the boot path.** Scales with the number of crewmates and the size of
  their templates, so it is kicked off the event loop and never gates
  readiness, the same way :func:`kiro_crew.dashboard.server_runtime.crewmate_prune._converge_channel_transcripts`
  runs its own startup pass.
"""

from __future__ import annotations

import dataclasses
import logging

from kiro_crew.acp.harness.kas import resolve_projected_spec
from kiro_crew.acp.kas_agents import build_kas_custom_agents
from kiro_crew.acp.mate_config import MateConfigError, has_mate_config, write_mate_config
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class MigrationReport:
    migrated: list[str] = dataclasses.field(default_factory=list)
    copy_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    equivalence_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    repoint_failed: dict[str, str] = dataclasses.field(default_factory=dict)
    already_owned: list[str] = dataclasses.field(default_factory=list)
    skipped_no_source: list[str] = dataclasses.field(default_factory=list)


def _candidates(cfg: KiroCrewConfig) -> dict[str, str]:
    """Crewmates whose ``kiro_agent`` still points at a shared template.

    Returns ``{member_name: source_template}``. A member record with no
    ``kiro_agent`` (nothing to migrate), one already resolving under its own
    id with an owned file (already migrated), or the reserved ``default``
    binding are excluded. ``member_id`` empty but ``kiro_agent`` set still
    qualifies: this migration is about the FILE a binding resolves to, not
    about whether the binding is a crewmate (see the RFC's §5.2 note that the
    two facts are orthogonal) -- a plain custom-agent binding that happens to
    share a name with its own id is vanishingly unlikely and harmless to copy.
    """
    out: dict[str, str] = {}
    for name, binding in cfg.agents.items():
        if name == "default":
            continue
        source = getattr(binding, "kiro_agent", "")
        if not source:
            continue
        if source == name and has_mate_config(name):
            continue
        out[name] = source
    return out


def _projected_tools_and_prompt(agents_dir, name: str, work_dir: str) -> dict:
    """The one entry :func:`build_kas_custom_agents` projects for *name*."""
    spec, prompt_dir = resolve_projected_spec(agents_dir, name, work_dir)
    return build_kas_custom_agents(prompt_dir, name, spec)[0]


def _equivalent(before: dict, after: dict) -> bool:
    """Whether two projected entries agree on everything the projection decides.

    ``id``/``name`` legitimately differ -- the repoint IS that difference.
    Every other key (``prompt``, ``tools``, ``mcpServers``, ``permissions``,
    ``resources``, ``includeMcpJson``, ``description``, ...) must match
    exactly, present-or-absent included.
    """
    skip = {"id", "name"}
    keys = (set(before) | set(after)) - skip
    return all(before.get(k) == after.get(k) for k in keys)


def migrate_existing_crewmates(
    cfg: KiroCrewConfig | None = None, *, work_dir: str = ""
) -> MigrationReport:
    """Run the Phase-2 migration once; safe to call on every boot (idempotent).

    Never raises: every failure is caught, logged into the returned report and
    left for the next boot to retry, because a crewmate that fails to migrate
    keeps working under the pre-RFC shared-pointer model.
    """
    report = MigrationReport()
    try:
        cfg = cfg or KiroCrewConfig.load()
        # Called through the module object, not bound by name at import time:
        # a module-level `from ... import kiro_agents_dir` copies the function
        # OBJECT, so a test's `monkeypatch.setattr(paths_mod, "kiro_agents_dir",
        # ...)` would never reach a locally-bound name.
        agents_dir = paths_mod.kiro_agents_dir()
        mates_dir = paths_mod.mate_agents_dir()
        # Never assume the two share a parent (RFC §6 guardrail: an isolated
        # instance can resolve them to different roots). Both must exist (or be
        # creatable) before any copy runs.
        mates_dir.mkdir(parents=True, exist_ok=True)
        candidates = _candidates(cfg)
    except Exception:
        logger.warning("mate-owned-config migration: could not enumerate candidates", exc_info=True)
        return report

    # Pass 1: write every candidate's copy and prove payload equivalence.
    # Pointers move only in pass 2, once every member here has PASSED, so a
    # migration bug in one crewmate's projection is caught before any binding
    # changes -- never a partial pointer-move the dry-run couldn't vouch for.
    verified: dict[str, str] = {}
    for name, source in candidates.items():
        try:
            before = _projected_tools_and_prompt(agents_dir, source, work_dir)
        except Exception as exc:
            report.skipped_no_source.append(name)
            logger.info(
                "mate-owned-config migration: %s's source %r unreadable: %s", name, source, exc
            )
            continue
        try:
            write_mate_config(name, source)
        except MateConfigError as exc:
            report.copy_failed[name] = str(exc)
            logger.warning("mate-owned-config migration: copy failed for %s: %s", name, exc)
            continue
        try:
            after = _projected_tools_and_prompt(mates_dir, name, work_dir)
        except Exception as exc:
            report.equivalence_failed[name] = f"copy unreadable: {exc}"
            logger.warning(
                "mate-owned-config migration: %s's copy could not be projected: %s", name, exc
            )
            continue
        if not _equivalent(before, after):
            report.equivalence_failed[name] = "projected payload changed"
            logger.warning(
                "mate-owned-config migration: %s's copy projects differently from "
                "its source %r; leaving the shared pointer in place",
                name,
                source,
            )
            continue
        verified[name] = source

    # Pass 2: repoint only the crewmates whose copy was proven equivalent.
    for name in verified:
        try:
            _repoint(name)
            report.migrated.append(name)
        except Exception as exc:
            report.repoint_failed[name] = str(exc)
            logger.warning("mate-owned-config migration: repoint failed for %s: %s", name, exc)

    if report.migrated:
        logger.info(
            "mate-owned-config migration: migrated %d existing crewmate(s): %s",
            len(report.migrated),
            ", ".join(sorted(report.migrated)),
        )
    return report


def _repoint(name: str) -> None:
    """Flip *name*'s ``kiro_agent`` to its own id, under the config lock.

    Mirrors :func:`kiro_crew.dashboard.agent_admin.crew_records._eager_copy_mate_config`'s
    write exactly (same optimistic lock via ``expected_store``,
    ``changed_fields={"kiro_agent"}``), re-reading the config fresh under the
    lock rather than reusing a snapshot a concurrent writer may have moved on.
    """
    from kiro_crew.memory_stores import persist_member_config

    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(name)
    if binding is None:
        raise MateConfigError(f"{name!r} is no longer a configured agent")
    prior_store = binding.memory_store
    binding.kiro_agent = name
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})
