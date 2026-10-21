"""Crewmate-owned agent config -- the eager copy of RFC *mate-owned-agent-config*.

A crewmate's config is a custom-agent JSON file in the EXACT format written to
``~/.kiro/agents/`` (no new schema), placed in a dedicated Crew-owned folder,
``~/.kiro/mates/agents/<id>.json`` (:func:`kiro_crew.config.paths.mate_agents_dir`) --
a SIBLING of ``~/.kiro/agents/`` under the kiro home, not under the data home.
The folder relocation is the whole mechanism: resolution reads it, the session
agent picker does not, so the crewmate owns an editable config without entering
the user-level agent catalog.

This module writes that file EAGERLY at crewmate creation (RFC §5.4): it reads
the source custom agent once, copies its payload verbatim, resolves ``prompt``
to literal text so no ``file://`` link back to the source survives, and writes
the crewmate file atomically at ``0o600``. There is no copy-on-first-edit fork,
no ``private_to`` lineage sidecar, and no live link to the source -- the copy is
a stamp, not a parent.

Crewmate-specific data (``member_id``, ``workspace``, ``memory_store``, avatar,
model) stays on the binding record (``agents.<name>`` in ``config.json``), where
it already lives; the file here is purely the KAS-facing custom agent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.agent_sdk import agent_specs
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import paths as paths_mod
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)


class MateConfigError(Exception):
    """A crewmate config file could not be written from its source agent."""


def _refuse_service_name(mate_id: str) -> None:
    """Refuse *mate_id* when it names the mate conductor or worker spec.

    Both are generated into the same folder as the crewmates' own files
    (``agent_files.MATE_SERVICE_AGENT_FILENAMES``), so a crewmate by either name
    would overwrite one of them.
    """
    from kiro_crew.agent_files import is_mate_service_agent

    if is_mate_service_agent(mate_id):
        raise MateConfigError(
            f"{mate_id!r} is the name of a spec Kiro Crew generates, so no crewmate may "
            "own a file by that name; rename the crewmate"
        )


def mate_config_path(mate_id: str) -> Path:
    """The crewmate's own custom-agent file, existing or not."""
    return paths_mod.mate_agents_dir() / f"{mate_id}.json"


def has_mate_config(mate_id: str) -> bool:
    """Whether *mate_id* has a crewmate config file in ``mates/agents/``.

    Never for the mate conductor or worker, whose generated specs share that folder.
    """
    from kiro_crew.agent_files import is_mate_service_agent

    return not is_mate_service_agent(mate_id) and mate_config_path(mate_id).is_file()


def source_agents_dir(source_agent: str) -> Path:
    """The folder *source_agent*'s spec is read from: the user level, except for
    the mate conductor and worker, which Crew generates into ``mates/agents/``."""
    from kiro_crew.agent_files import is_mate_service_agent

    if is_mate_service_agent(source_agent):
        return paths_mod.mate_agents_dir()
    return paths_mod.kiro_agents_dir()


def _resolved_source_spec(source_agent: str) -> dict[str, Any]:
    """Read *source_agent*'s user-level spec with ``prompt`` inlined as literal text.

    The source is a user-level custom agent (``~/.kiro/agents/``), read once
    through the hardened reader :func:`kiro_crew.agent_sdk.agent_specs.load_agent_spec`. Its ``prompt``
    is resolved against that same directory, so a ``file://`` reference becomes
    the literal prompt text and the crewmate file carries no path back into the
    source. A prompt-less source resolves to the KAS fallback -- the same text a
    session on the source would run -- so the copy and the source project
    identically (RFC Phase-2 payload equivalence). The mate conductor and worker
    are read from the crewmate folder instead (:func:`source_agents_dir`).
    """
    agents_dir = source_agents_dir(source_agent)
    try:
        spec = agent_specs.load_agent_spec(agents_dir, source_agent)
    except agent_specs.AgentSpecUnreadable as exc:
        raise MateConfigError(
            f"source agent {source_agent!r} has no readable spec to copy: {exc}"
        ) from exc
    # A deep copy the caller owns: load_agent_spec already deep-copies, but the
    # resolve + reassign below mutates, so never risk touching a memoised parse.
    spec = copy_json_document(spec)
    try:
        prompt = agent_specs.resolve_prompt(spec, agent_id=source_agent, agents_dir=agents_dir)
    except agent_specs.AgentSpecUnreadable as exc:
        raise MateConfigError(
            f"source agent {source_agent!r} prompt could not be inlined: {exc}"
        ) from exc
    spec["prompt"] = prompt
    return spec


def write_mate_config(mate_id: str, source_agent: str) -> Path:
    """Eagerly copy *source_agent*'s config into the crewmate file for *mate_id*.

    Returns the written path. The payload is the source spec verbatim -- every
    field the projection honours, present and future, with no field allow-list to
    keep in sync -- except ``prompt``, which is inlined to literal text so no
    ``file://`` link back to the source survives. ``name`` is set to *mate_id* so
    the file DECLARES the id it is resolved under, matching the
    declared-name-wins rule the resolver applies.

    Written atomically at ``0o600``. Overwrites an existing crewmate file: a
    re-create or a migration re-stamp is a fresh copy, not an error.
    """
    spec = _resolved_source_spec(source_agent)
    # The file declares the id it is resolved under. load_agent_spec prefers a
    # spec whose declared name matches the id, so a copied ``name`` that still
    # read as the SOURCE would make the crewmate file resolve under the source's
    # name, not the crewmate's -- set it to the crewmate id.
    spec["name"] = mate_id
    path = write_mate_spec(mate_id, spec)
    logger.info("wrote crewmate config %s copied from source agent %r", path, source_agent)
    return path


def write_mate_spec(mate_id: str, spec: dict[str, Any]) -> Path:
    """Write *spec* as *mate_id*'s crewmate file and publish it to the dispatch snapshot.

    The one writer every creation path goes through (the create route, the
    command line, the first crewmate and the startup migration). Publishing in
    the same call is what puts the crewmate's next chat on the conductor: the
    chat agent is chosen from the in-memory snapshot
    (:func:`kiro_crew.mate_worker.is_mate_owned_agent`), and a file the snapshot
    does not know yet would run as the chat itself, with every tool the copied
    template carries. Written atomically at ``0o600``.
    """
    from kiro_crew.config.loader import publish_mate_agents
    from kiro_crew.mate_worker import drop_partial_platform_servers

    _refuse_service_name(mate_id)
    # A platform server comes from the gateway; an entry that only switches one
    # on or off would be a server with no transport in this file.
    drop_partial_platform_servers(spec)
    path = mate_config_path(mate_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(spec, indent=2), fsync=True, mode=0o600)
    except OSError as exc:
        raise MateConfigError(
            f"could not write crewmate config for {mate_id!r} at {path}: {exc}"
        ) from exc
    publish_mate_agents([mate_id])
    return path


def adopt_own_file(name: str) -> str:
    """Give crewmate *name* its own file and point its binding at it. Blocking.

    The eager copy (``rfc-mate-owned-agent-config`` §5.4) every creation path
    runs: the dashboard create route through
    ``agent_admin.crew_records._eager_copy_mate_config`` and the command line
    directly. Reads whatever ``kiro_agent`` resolves to now, copies it with
    :func:`write_mate_config`, records the worker kit for a template other than
    the default one, and repoints ``kiro_agent`` to *name*. Returns an outcome
    code: ``copied``, ``no_binding``, ``no_source``, ``already_owned`` or
    ``switched_off`` (Crewmate 2.0 is off, :mod:`kiro_crew.crewmate_v2`: nothing is
    written and the binding keeps its template); raises :class:`MateConfigError`
    when the copy cannot be written.
    """
    from kiro_crew import crewmate_v2
    from kiro_crew.agent_files import is_kirocrew_default_agent
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.mate_worker_kit import record_initial_kit
    from kiro_crew.memory_stores import persist_member_config

    if not crewmate_v2.enabled():
        return "switched_off"
    _invalidate_config_cache()
    cfg = KiroCrewConfig.load()
    binding = cfg.agents.get(name)
    if binding is None:
        return "no_binding"
    source = binding.kiro_agent
    if not source:
        return "no_source"
    if source == name:
        # Already owns its id (a re-create path, or already migrated).
        return "already_owned"
    prior_store = binding.memory_store
    write_mate_config(name, source)
    # A template other than the default is the worker's extra kit: record the
    # copy, so a later "Re-copy from template" can tell the kit from what was
    # added since. Best-effort; the file is already written.
    if not is_kirocrew_default_agent(source):
        record_initial_kit(name, source)
    binding.kiro_agent = name
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})
    # Named in ``migrated.json`` with the agent it was created on, like a migrated
    # crewmate, so turning Crewmate 2.0 off puts it back on that agent
    # (``mate_config_migration.reverse_migration``). Best-effort.
    try:
        from kiro_crew.mate_config_migration import _record_migrated

        _record_migrated(name, source)
    except OSError:
        logger.warning("crewmate %s: not recorded in migrated.json", name, exc_info=True)
    return "copied"
