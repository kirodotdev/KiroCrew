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

from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import paths as paths_mod
from kiro_crew.user_json import copy_json_document

logger = logging.getLogger(__name__)


class MateConfigError(Exception):
    """A crewmate config file could not be written from its source agent."""


def mate_config_path(mate_id: str) -> Path:
    """The crewmate's own custom-agent file, existing or not."""
    return paths_mod.mate_agents_dir() / f"{mate_id}.json"


def has_mate_config(mate_id: str) -> bool:
    """Whether *mate_id* has a crewmate config file in ``mates/agents/``."""
    return mate_config_path(mate_id).is_file()


def _resolved_source_spec(source_agent: str) -> dict[str, Any]:
    """Read *source_agent*'s user-level spec with ``prompt`` inlined as literal text.

    The source is a user-level custom agent (``~/.kiro/agents/``), read once
    through the hardened reader :func:`kas_agents.load_agent_spec`. Its ``prompt``
    is resolved against that same directory, so a ``file://`` reference becomes
    the literal prompt text and the crewmate file carries no path back into the
    source. A prompt-less source resolves to the KAS fallback -- the same text a
    session on the source would run -- so the copy and the source project
    identically (RFC Phase-2 payload equivalence).
    """
    agents_dir = paths_mod.kiro_agents_dir()
    try:
        spec = kas_agents_mod.load_agent_spec(agents_dir, source_agent)
    except kas_agents_mod.KasAgentTranslationError as exc:
        raise MateConfigError(
            f"source agent {source_agent!r} has no readable spec to copy: {exc}"
        ) from exc
    # A deep copy the caller owns: load_agent_spec already deep-copies, but the
    # resolve + reassign below mutates, so never risk touching a memoised parse.
    spec = copy_json_document(spec)
    try:
        prompt = kas_agents_mod.resolve_prompt(spec, agent_id=source_agent, agents_dir=agents_dir)
    except kas_agents_mod.KasAgentTranslationError as exc:
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
    path = mate_config_path(mate_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(spec, indent=2), fsync=True, mode=0o600)
    except OSError as exc:
        raise MateConfigError(
            f"could not write crewmate config for {mate_id!r} at {path}: {exc}"
        ) from exc
    logger.info("wrote crewmate config %s copied from source agent %r", path, source_agent)
    return path
