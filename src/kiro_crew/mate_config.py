"""Crewmate-owned agent config -- the eager copy of RFC *mate-owned-agent-config*.

The file is a custom-agent JSON in the ``~/.kiro/agents/`` format, kept in the
Crew-owned ``~/.kiro/mates/agents/<id>.json`` (:func:`kiro_crew.config.paths.mate_agents_dir`),
which resolution reads and the agent picker does not. It is written EAGERLY at
creation (RFC §5.4): the source agent copied once, its prompt inlined so no
``file://`` link survives, written atomically at ``0o600``. A stamp, not a parent.
Crewmate data (``member_id``, workspace, store, avatar, model) stays on the
``agents.<name>`` binding in ``config.json``.
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


def user_agent_named(name: str) -> bool:
    """Whether a user-level agent declares *name*. Blocking.

    A crewmate's own file is published under its name only when no user-level
    agent declares it (``publish_mate_agents``, ``mate_worker.mate_file_for_member``),
    so a crewmate of that name would be bound to someone's custom agent instead of
    its copy. Every create path refuses the name.
    """
    from kiro_crew.agent_sdk import agent_specs

    return bool(name) and not agent_specs.agent_spec_absent(paths_mod.kiro_agents_dir(), name)


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

    Read once through ``agent_specs.load_agent_spec``; a ``file://`` prompt becomes
    literal text and a prompt-less one the KAS fallback, so the copy projects as
    the source does. The mate service specs are read from the crewmate folder
    (:func:`source_agents_dir`).
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

    The source spec verbatim, no field allow-list, except a literal ``prompt`` and
    ``name`` set to *mate_id* (declared-name-wins). Atomic at ``0o600``; an
    existing crewmate file is replaced.
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


#: Which instance wrote each crewmate file: ``{"<mate id>": "<data home>"}``.
_OWNERS_FILE = "owners.json"


def _owners_path() -> Path:
    return paths_mod.mate_agents_dir().parent / _OWNERS_FILE


def _this_home() -> str:
    return str(paths_mod.data_home().resolve())


def _read_owners() -> dict[str, str]:
    """The owner record. Raises :class:`MateConfigError` when it exists but is unreadable."""
    try:
        raw = json.loads(_owners_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise MateConfigError(f"the crewmate owner record {_owners_path()} is unreadable") from exc
    if not isinstance(raw, dict):
        raise MateConfigError(f"the crewmate owner record {_owners_path()} is not an object")
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _refuse_foreign_file(mate_id: str, owners: dict[str, str]) -> None:
    """Refuse to replace *mate_id*'s file when another instance wrote it.

    The crewmate folder sits under the Kiro home, which instances with separate
    data homes can share, so the same crewmate name in two of them names one
    file. A file this instance wrote (or one with no owner on record, from
    before the record) may be replaced; one another instance wrote may not.
    """
    owner = owners.get(mate_id)
    if owner and owner != _this_home() and mate_config_path(mate_id).is_file():
        raise MateConfigError(
            f"another Kiro Crew instance ({owner}) already keeps a crewmate file named "
            f"{mate_id!r} in this Kiro home; rename the crewmate"
        )


def write_mate_spec(mate_id: str, spec: dict[str, Any]) -> Path:
    """Write *spec* as *mate_id*'s crewmate file and publish it to the dispatch snapshot.

    The one writer of every creation path. Publishing in the same call puts the
    next chat on the conductor (:func:`kiro_crew.mate_worker.is_mate_owned_agent`
    reads the snapshot). Atomic at ``0o600``; refuses another instance's file
    (:func:`_refuse_foreign_file`).
    """
    from kiro_crew.config.loader import publish_mate_agents
    from kiro_crew.mate_worker import drop_partial_platform_servers

    _refuse_service_name(mate_id)
    owners = _read_owners()
    _refuse_foreign_file(mate_id, owners)
    # A platform server comes from the gateway; an entry that only switches one
    # on or off would be a server with no transport in this file.
    drop_partial_platform_servers(spec)
    path = mate_config_path(mate_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(spec, indent=2), fsync=True, mode=0o600)
        if owners.get(mate_id) != _this_home():
            owners[mate_id] = _this_home()
            atomic_write(
                _owners_path(), json.dumps(owners, indent=2, sort_keys=True) + "\n", mode=0o600
            )
    except OSError as exc:
        raise MateConfigError(
            f"could not write crewmate config for {mate_id!r} at {path}: {exc}"
        ) from exc
    publish_mate_agents([mate_id])
    return path


def adopt_own_file(name: str) -> str:
    """Give crewmate *name* its own file and point its binding at it. Blocking.

    The eager copy (RFC §5.4). Returns ``copied``, ``no_binding``, ``no_source``
    or ``already_owned``; raises :class:`MateConfigError` when it cannot write.
    """
    from kiro_crew.agent_files import is_kirocrew_default_agent
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
    from kiro_crew.mate_worker_kit import record_initial_kit
    from kiro_crew.memory_stores import persist_member_config

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
    if user_agent_named(name):
        # The binding would name a user-level agent, and the crewmate would run it.
        raise MateConfigError(
            f"a user-level agent is already named {name!r}, so the crewmate's own file "
            "could not be used under that name; rename the crewmate"
        )
    from kiro_crew.mate_config_migration import uncarried_restrictions

    lost = uncarried_restrictions(_resolved_source_spec(source))
    if lost:
        # The worker would run without the template's own limits; the crewmate
        # keeps running as the template, which still applies them.
        raise MateConfigError(
            f"{source!r} sets {' and '.join(lost)} limits its worker cannot carry, so "
            f"{name!r} keeps running as {source!r}"
        )
    prior_store = binding.memory_store
    write_mate_config(name, source)
    # A template other than the default is the worker's extra kit: record the
    # copy, so a later "Re-copy from template" can tell the kit from what was
    # added since. Best-effort; the file is already written.
    if not is_kirocrew_default_agent(source):
        record_initial_kit(name, source)
    binding.kiro_agent = name
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})
    return "copied"
