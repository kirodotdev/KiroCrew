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

``kirocrew-worker`` is reserved like every other managed name. The derived-spec gate
(:func:`kiro_crew.agent.require_fresh_derived_spec`) refuses a top-level copy of it on
the worker's own spawn, but it scans the top level only and compares the name
exactly, so a nested or case-variant claim is left to this check.

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
from kiro_crew import agent_discovery, hooks
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
from kiro_crew.agent_spec_format import (
    is_agent_spec_name,
    is_native_skill_alias_name,
    spec_stem,
)
from kiro_crew.config.paths import project_agents_dir
from kiro_crew.security import is_unverifiable_path_refusal, sensitive_path_refusal

#: Managed agent names a checkout may not claim.
SHADOW_REFUSED_AGENT_NAMES: frozenset[str] = frozenset(
    spec_stem(name) for name in OWNED_KIRO_AGENT_FILES
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


#: How many spec files, and how many directories, one scan examines. A checkout's
#: agents tree is small; the caps keep a pathological one (or a linked directory
#: that reaches a large tree) from stalling the spawn, and hitting either refuses.
_MAX_SPEC_FILES = 2000
_MAX_SPEC_DIRS = 500


class _TooManySpecs(Exception):
    pass


class _Unverifiable(Exception):
    """An entry this scan cannot read but kiro-cli may still load; ``args[0]`` is its path."""


def _dir_key(directory: Path) -> tuple[int, int] | None:
    try:
        st = os.stat(directory)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _screened_target(path: Path) -> Path | None:
    """Where *path* leads once every link in it is screened, or ``None`` when refused.

    :func:`kiro_crew.hooks.validate_file_path` reads a Windows link's own target
    and refuses an untrusted UNC share before anything follows the link, so asking
    here never opens an outbound SMB connection; it then canonicalises the path and
    applies the sensitive-path fence. Asked before any probe that follows a link:
    the agents directory itself, and every link inside it.
    """
    screened = hooks.validate_file_path(str(path))
    return None if screened is None else Path(screened)


def _is_link(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_symlink() or entry.is_junction()
    except OSError:
        return False


def _project_spec_files(project_agents: Path) -> list[Path]:
    """Every spec-shaped file under *project_agents*, nested ones included.

    No roster filtering: discovery leaves some files out (skill-view aliases, for
    one) because they are not agents a user should be offered, but kiro-cli applies
    no such filter when it loads the directory, so a file the roster hides can still
    be one that runs. Nested directories are walked because kiro-cli loads them too,
    and a linked directory is followed because kiro-cli follows it. Each directory
    is walked once, keyed by its device and inode, so a link loop ends.

    No entry is followed before :func:`_screened_target` has passed it: a link it
    refuses (an untrusted share, a protected location) raises :class:`_Unverifiable`
    unlisted, because kiro-cli would still load what it reaches. A link that passes
    and leads to a directory is walked at its screened target; any other link is
    kept as an entry under its own name, for the spec reader to judge. An entry that
    cannot be stat'ed (a symlink loop, say) is kept rather than dropped, so it cannot
    hide the rest and a name it claims by its stem still counts.
    """
    files: list[Path] = []
    pending = [project_agents]
    visited: set[tuple[int, int]] = set()
    while pending:
        directory = pending.pop()
        key = _dir_key(directory)
        if key is not None:
            if key in visited:
                continue
            visited.add(key)
        if len(visited) > _MAX_SPEC_DIRS:
            raise _TooManySpecs
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if _is_link(entry):
                        target = _screened_target(Path(entry.path))
                        if target is None:
                            raise _Unverifiable(Path(entry.path))
                        if os.path.isdir(target):
                            pending.append(target)
                            continue
                    else:
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


def _reserved_claim(spec: Path) -> str | None:
    """The reserved name *spec* claims by its stem or its declared name, if any.

    Compared case-insensitively, because a case-insensitive filesystem makes
    ``KIROCREW.json`` and ``kirocrew.json`` one file.

    What a file declares is read through the strict hardened reader, and its two
    failure classes get different answers:

    * Bytes that were read but do not parse as a spec object claim nothing: a
      broken JSON document, a non-UTF-8 byte, a Markdown file with no frontmatter
      (a README), a document nested past the parser's depth. kiro-cli's default
      engine, which both guarded spawn paths run, offers no mode for any of them
      either. Its v3 engine decodes a non-UTF-8 byte leniently and parses deeper
      documents, so it would load some of these; a move to that engine must revisit
      this rule.
    * A file whose bytes could not be read raises :class:`_Unverifiable`: a
      hardlinked spec, which kiro-cli loads although the hardened reader refuses
      it; a link to a protected target; a file past the size cap. A filename is
      not evidence of what such a file declares.

    A ``._`` file is refused by the hardened reader on its name alone, but kiro-cli
    loads one that parses, under the name it declares. Only a real AppleDouble
    sidecar, recognised by its magic bytes (it opens with a NUL byte that neither
    spec form can start with), claims nothing; any other ``._`` file is unverifiable.
    """
    stem = spec_stem(spec.name)
    if _is_reserved(stem):
        return stem
    if spec.name.startswith("._"):
        if agent_discovery.apple_double_sidecar(spec):
            return None
        raise _Unverifiable(spec)
    try:
        data = agent_discovery.read_agent_spec_strict(
            spec, operation="managed_agent_shadow", source="unknown"
        )
    except agent_discovery.SensitiveAgentSpecPathError:
        raise _Unverifiable(spec) from None
    except ValueError as exc:
        if isinstance(exc.__cause__, hooks.FileTooLargeError):
            raise _Unverifiable(spec) from None
        return None
    except OSError:
        raise _Unverifiable(spec) from None
    except RecursionError:
        return None
    if not isinstance(data, dict):
        return None
    declared = agent_discovery.spec_str(data, "name", stem)
    return declared if _is_reserved(declared) else None


def _is_reserved(name: str) -> bool:
    folded = name.casefold()
    return folded in _RESERVED_FOLDED or is_native_skill_alias_name(folded)


def _link_refusal(path: Path) -> str:
    return (
        f"the session's project links {path}, in or on the way to its agents directory, "
        "to an untrusted share or a protected location, so Kiro Crew cannot confirm "
        "that what it reaches does not replace an agent Kiro Crew installs. Remove that "
        "link to run a session there."
    )


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

    Every project form counts, JSON and Markdown alike. A spec whose bytes cannot be
    read refuses too, unless its filename already claims a reserved name: the
    question is whether the checkout claims a reserved name at all, and a file
    nobody could read may claim one. A file whose bytes were read but are not a spec
    claims nothing (see :func:`_reserved_claim`). A protected checkout
    answers "no shadow", the direction every other project scan takes, and so does
    one whose agents directory cannot be listed; a cwd the sensitive-path resolver
    could not check refuses instead. A link inside the agents tree, or
    on the way to it, that leads to an untrusted share or a protected location
    refuses, unlisted and unfollowed.
    """
    if not agent or not work_dir:
        return None
    # Decided before any filesystem access under the checkout, like every other
    # reader of a caller-supplied project scope. Only a confirmed match is the
    # protected-checkout exemption: a resolver that stalled has confirmed nothing,
    # and admitting on it would skip the scan for whatever checkout it stalled on.
    protected = sensitive_path_refusal(str(work_dir))
    if protected is not None:
        if is_unverifiable_path_refusal(protected):
            return (
                f"the session's project path {work_dir} could not be checked against the "
                "protected locations, so Kiro Crew cannot scan it for an agent that "
                "replaces one Kiro Crew installs. Retry once the filesystem responds."
            )
        return None
    project_agents = project_agents_dir(work_dir)
    # Screened before ``samefile`` and the scan stat it: either would follow a
    # linked ``.kiro`` or ``agents`` to wherever it points.
    if _screened_target(project_agents) is None:
        return _link_refusal(project_agents)
    if _is_installed_agents_dir(project_agents):
        return None
    try:
        files = _project_spec_files(project_agents)
    except _TooManySpecs:
        return (
            f"the session's project holds more than {_MAX_SPEC_FILES} agent specs or "
            f"{_MAX_SPEC_DIRS} directories under {project_agents}, too many to check that "
            "none of them replaces an agent Kiro Crew installs; reduce them to run a "
            "session there."
        )
    except _Unverifiable as exc:
        return _link_refusal(exc.args[0])
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
