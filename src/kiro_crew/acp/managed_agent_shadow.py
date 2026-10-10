"""Refuse a kiro-cli session in a checkout that claims an agent name Kiro Crew owns.

kiro-cli resolves ``--agent <name>`` against ``<cwd>/.kiro/agents`` BEFORE
``~/.kiro/agents``, and the kiro spawn paths hand it only the name. So when a
checkout ships a spec that kiro-cli would select for a managed name -- one that
declares the name, or one whose filename stem is the name -- the child runs that
file in place of the spec Kiro Crew generated, with whatever ``allowedTools`` it
declares. Those grants never reach the PreToolUse gate, and the same swap can just
as easily DROP a restriction the installed spec carries, so the risk runs in both
directions.

A managed name is one Kiro Crew writes under ``~/.kiro/agents``
(:data:`kiro_crew.agent_files.OWNED_KIRO_AGENT_FILES`). For those names the
product is the author, so a project copy is never what the operator selected.
Project specs under any other name stay the documented discovery feature and are
untouched here.

Refused rather than validated, the posture the fork gate and the worker gate take
for their own names: nothing in this product writes into a project directory, so a
project copy can never be re-derived or brought under the governance ceiling, and a
check whose every answer would be "refuse" is more honestly written as a refusal.
Rewriting the file would be Crew editing a repository's tracked content, so there
is no repair path and no override.

``kirocrew-worker`` is excluded because
:func:`kiro_crew.agent.require_fresh_derived_spec` refuses its shadow on every path
that spawns it, with a message that names its own remedy.

The skill-view aliases are reserved the same way. With native skill projection
on, the name a spawn finally hands to ``--agent`` is an alias Kiro Crew generates
and publishes under ``~/.kiro/agents``, not the agent's own name, and kiro-cli
resolves that alias against the checkout first as well. An alias is always Crew's
own file, so a checkout may not claim one either.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew import agent as agent_mod
from kiro_crew import agent_discovery
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES, WORKER_AGENT_FILENAME
from kiro_crew.agent_spec_format import (
    is_agent_spec_name,
    is_native_skill_alias_name,
    spec_stem,
)
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.security import is_sensitive_path

#: Managed agent names a checkout may not claim.
SHADOW_REFUSED_AGENT_NAMES: frozenset[str] = frozenset(
    spec_stem(name) for name in OWNED_KIRO_AGENT_FILES if name != WORKER_AGENT_FILENAME
)
_RESERVED_FOLDED = frozenset(name.casefold() for name in SHADOW_REFUSED_AGENT_NAMES)


def _is_installed_agents_dir(project_agents: Path) -> bool:
    """Is the checkout's agents directory the one Kiro Crew installs into?

    A session whose cwd is the home directory has ``<cwd>/.kiro/agents`` equal to
    ``~/.kiro/agents``, so the installed spec would read as its own shadow.
    """
    try:
        return os.path.samefile(project_agents, agent_mod.kiro_agents_dir_path())
    except OSError:
        return False


#: How many spec files one scan examines. A checkout's agents tree is small; a cap
#: keeps a pathological one from stalling the spawn, and hitting it refuses.
_MAX_SPEC_FILES = 2000


class _TooManySpecs(Exception):
    pass


def _project_spec_files(project_agents: Path) -> list[Path]:
    """Every spec-shaped file under *project_agents*, nested ones included.

    No roster filtering: discovery leaves some files out (skill-view aliases, for
    one) because they are not agents a user should be offered, but kiro-cli applies
    no such filter when it loads the directory, so a file the roster hides can still
    be one that runs. Nested directories are walked because kiro-cli loads them too.
    Symlinked directories are not followed. An entry that cannot be stat'ed (a
    symlink loop, say) is kept rather than dropped, so it cannot hide the rest and a
    name it claims by its stem still counts.
    """
    files: list[Path] = []
    pending = [project_agents]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        is_dir = False
                    if is_dir:
                        pending.append(Path(entry.path))
                        continue
                    # Case-insensitive, as discovery is: ``kirocrew.JSON`` is a spec.
                    if not is_agent_spec_name(entry.name):
                        continue
                    files.append(Path(entry.path))
                    if len(files) > _MAX_SPEC_FILES:
                        raise _TooManySpecs
        except OSError:
            continue
    return sorted(files)


class _Unverifiable(Exception):
    pass


def _reserved_claim(spec: Path) -> str | None:
    """The reserved name *spec* claims by its stem or its declared name, if any.

    Compared case-insensitively, because a case-insensitive filesystem makes
    ``KIROCREW.json`` and ``kirocrew.json`` one file.

    Raises :class:`_Unverifiable` when the declared name cannot be read. The hardened
    reader refuses some files kiro-cli would still load (a hardlinked spec, for one),
    and a filename stands in for a name the file may well declare otherwise, so a
    declaration nobody could read is not evidence that the file claims nothing.
    """
    stem = spec_stem(spec.name)
    if _is_reserved(stem):
        return stem
    declared = agent_discovery._declared_project_agent_name(spec)
    if declared is None:
        raise _Unverifiable
    return declared if _is_reserved(declared) else None


def _is_reserved(name: str) -> bool:
    folded = name.casefold()
    return folded in _RESERVED_FOLDED or is_native_skill_alias_name(folded)


def managed_agent_shadow_refusal(agent: str | None, work_dir: str | Path | None) -> str | None:
    """Why a kiro-cli session in *work_dir* must not start, or ``None`` to admit it.

    *work_dir* must be the cwd the kiro-cli child runs with, because that is the
    directory it loads project agents from; any other directory would check a
    checkout nobody runs in.

    The answer does not depend on *agent* beyond its presence. kiro-cli loads every
    project agent at startup, and a shared runtime can later switch to another agent
    by ``session/set_mode``, so a checkout that claims ANY reserved name is refused
    whichever agent the spawn names. The spawn may also hand kiro-cli a skill-view
    alias in place of the agent's own name, which is why those are reserved too.

    Every project form counts, JSON and Markdown alike. A spec whose declared name
    cannot be read refuses too, unless its filename already claims a reserved name:
    the question is whether the checkout claims a reserved name at all, and a file
    nobody could read may claim one. A protected checkout answers "no shadow", the
    direction every other project scan takes, and so does one whose agents directory
    cannot be listed.
    """
    if not agent or not work_dir:
        return None
    # Decided before any filesystem access under the checkout, like every other
    # reader of a caller-supplied project scope.
    if is_sensitive_path(str(work_dir)):
        return None
    project_agents = project_agents_dir(work_dir)
    if _is_installed_agents_dir(project_agents):
        return None
    try:
        files = _project_spec_files(project_agents)
    except _TooManySpecs:
        return (
            f"the session's project holds more than {_MAX_SPEC_FILES} agent specs under "
            f"{project_agents}, too many to check that none of them replaces an agent "
            "Kiro Crew installs; reduce them to run a session there."
        )
    for spec in files:
        try:
            claimed = _reserved_claim(spec)
        except _Unverifiable:
            return (
                f"the session's project holds an agent spec at {spec} whose declared name "
                "could not be read, so Kiro Crew cannot confirm it does not replace an "
                "agent Kiro Crew installs. Fix or remove that file to run a session there."
            )
        if claimed is None:
            continue
        return (
            f"the session's project declares its own {claimed!r} agent spec at {spec}, "
            "and kiro-cli loads a project spec ahead of the one Kiro Crew installs, so "
            "that copy is what would run -- with grants and restrictions Kiro Crew never "
            "wrote and cannot govern. Remove that project spec, or give it a name and a "
            "filename of its own, to run the installed agent."
        )
    return None
