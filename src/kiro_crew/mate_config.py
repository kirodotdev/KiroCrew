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

import contextlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
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


#: Why a create naming another template is refused (:func:`start_template_refusal`).
MATE_TEMPLATE_UNSUPPORTED = (
    "Mates start from the built-in worker; create it, then add abilities from that "
    "agent in its editor."
)


def start_template_refusal(kiro_agent: str, key: str) -> str:
    """The refusal for creating crew *key* from template *kiro_agent*, else ``""``.

    :func:`adopt_own_file` writes the source-free worker file and reads no
    template, so a template's tools, restrictions, servers, skills and prompt
    would not reach the crewmate. A create may therefore name only the built-in
    template (``DEFAULT_CREWMATE_TEMPLATE``) or *key* itself, the crew's config
    key, which :func:`adopt_own_file` leaves bound to that template. Both create
    doors (``POST /api/agents``, ``kirocrew agent create``) ask this before
    writing anything.
    """
    from kiro_crew.change_card_catalog_kinds import DEFAULT_CREWMATE_TEMPLATE

    if kiro_agent in (DEFAULT_CREWMATE_TEMPLATE, key):
        return ""
    return MATE_TEMPLATE_UNSUPPORTED


def mate_config_path(mate_id: str) -> Path:
    """The crewmate's own custom-agent file, existing or not."""
    return paths_mod.mate_agents_dir() / f"{mate_id}.json"


def has_mate_config(mate_id: str) -> bool:
    """Whether *mate_id* has a crewmate config file in ``mates/agents/``.

    Never for the mate conductor or worker, whose generated specs share that folder.
    """
    from kiro_crew.agent_files import is_mate_service_agent

    return not is_mate_service_agent(mate_id) and mate_config_path(mate_id).is_file()


def crewmate_file_on_record(mate_id: str) -> bool:
    """Whether THIS instance ever wrote a crewmate file *mate_id* (``owners.json``).

    Every crewmate file write records its id with the writer's data home and
    nothing removes it, so this still answers True after the file is deleted.
    An entry another instance sharing the Kiro home wrote is not this one's.
    Raises :class:`MateConfigError` when the record exists but cannot be read.
    """
    return _read_owners().get(mate_id) == _this_home()


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


def write_mate_config(mate_id: str) -> Path:
    """Write *mate_id*'s crewmate file as the RFC §5.4 minimal file: its identity
    and empty additions.

    The worker base (``kirocrew-mate-worker``) and the chat conductor
    (``kirocrew-mate-conductor``) are source-defined, so a new mate gets its
    baseline capabilities from them at projection time, and the file holds only
    what the owner later adds (Add abilities). No agent file on disk is read: a
    new crewmate is the source-defined worker exactly, so neither the machine's
    own ``~/.kiro/settings/mcp.json`` servers nor a template's prompt, servers,
    skills or limits reach it. Atomic at ``0o600``; an existing crewmate file is
    replaced.
    """
    spec: dict[str, Any] = {
        # Identity: the file declares the id it resolves under (declared-name-wins),
        # so the crewmate file resolves under the crewmate.
        "name": mate_id,
        "prompt": "",
        "mcpServers": {},
        "tools": [],
        "allowedTools": [],
        "skills": [],
        "resources": [],
    }
    path = write_mate_spec(mate_id, spec)
    logger.info("wrote minimal crewmate config %s", path)
    return path


def _carried_restrictions(mate_id: str, source: dict[str, Any]) -> dict[str, Any]:
    """*source*'s limits in crewmate-file shape: ``excludedTools`` and base-server switch-offs.

    Read from :func:`mate_config_migration.migration_file`, keeping only its
    restriction keys: ``excludedTools`` and each ``{"disabled": true}`` entry for a
    base server. Every per-server ``disabledTools`` the source names also joins
    ``excludedTools``, base server or not: the file does not copy a server entry
    verbatim, so a limit left inside one would be lost. With the worker base
    unreadable, every exclusion and per-server tool limit is still kept as
    ``excludedTools``, and every server it
    switches off is kept as ``{"disabled": true}``: the base is generated at boot,
    so a switch-off dropped now would launch that server once the base is back. A
    switch-off naming no base server launches nothing. A failure here fails the
    add rather than writing a file without the template's limits.
    """
    from kiro_crew.mate_config_migration import _worker_base, migration_file
    from kiro_crew.mate_worker import disabled_tool_refs
    from kiro_crew.mcp_cleanup import mcp_entry_is_muted

    tool_limits = disabled_tool_refs(source.get("mcpServers"))
    try:
        base = _worker_base()
    except Exception:  # noqa: BLE001 - an unreadable base keeps every named limit
        logger.warning("worker base unreadable; carrying every limit the template names")
        raw = source.get("excludedTools")
        excluded = [t for t in raw if isinstance(t, str)] if isinstance(raw, list) else []
        excluded += tool_limits
        out_fallback: dict[str, Any] = {}
        if excluded:
            out_fallback["excludedTools"] = list(dict.fromkeys(excluded))
        servers = source.get("mcpServers")
        muted = {
            name: {"disabled": True}
            for name, entry in (servers if isinstance(servers, dict) else {}).items()
            if isinstance(name, str) and isinstance(entry, dict) and mcp_entry_is_muted(entry)
        }
        if muted:
            out_fallback["mcpServers"] = muted
        return out_fallback
    full = migration_file(mate_id, base, source)
    out: dict[str, Any] = {}
    kept = [t for t in (full.get("excludedTools") or []) if isinstance(t, str)]
    excluded_all = list(dict.fromkeys([*kept, *tool_limits]))
    if excluded_all:
        out["excludedTools"] = excluded_all
    switched_off = {
        name: entry
        for name, entry in (full.get("mcpServers") or {}).items()
        if entry == {"disabled": True}
    }
    if switched_off:
        out["mcpServers"] = switched_off
    return out


#: Which instance wrote each crewmate file: ``{"<mate id>": "<data home>"}``.
_OWNERS_FILE = "owners.json"


def _owners_path() -> Path:
    return paths_mod.mate_agents_dir().parent / _OWNERS_FILE


@contextlib.contextmanager
def _owners_lock() -> Iterator[None]:
    """Hold the cross-process lock over the owner record. Raises ``OSError`` past its ceiling."""
    lock_path = _owners_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with platform_compat.open_lock_file(lock_path) as fd, platform_compat.flock_exclusive(fd):
        yield


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
    # A platform server comes from the gateway; an entry that only switches one
    # on or off would be a server with no transport in this file.
    drop_partial_platform_servers(spec)
    path = mate_config_path(mate_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Two instances sharing this Kiro home can write the same name at once
        # (both migrating after an upgrade). The owner check and both writes run
        # under one cross-process lock, so the second sees the first's owner.
        with _owners_lock():
            owners = _read_owners()
            _refuse_foreign_file(mate_id, owners)
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

    The eager create (RFC §5.4): writes the minimal, source-free file
    (:func:`write_mate_config`) and repoints ``kiro_agent`` from the binding's
    starting template to *name*; the template's own file is not read. Returns
    ``copied``, ``no_binding``, ``no_source`` or ``already_owned``; raises
    :class:`MateConfigError` when it cannot write.
    """
    from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
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
    prior_store = binding.memory_store
    write_mate_config(name)
    binding.kiro_agent = name
    persist_member_config(cfg, name, expected_store=prior_store, changed_fields={"kiro_agent"})
    return "copied"
