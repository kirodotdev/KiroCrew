"""Which skills a KAS session for an agent offers, read from the spec KAS runs.

The application side (``remote_session.skills_copy``) needs the skill scope of a
KAS session before the engine starts, and must reach the ACP layer through this
SDK. The spec is picked in the order ``acp/harness/kas.session_extras`` picks it:
a derived agent's freshly re-derived snapshot, an app-bound file, then
``resolve_projected_spec`` (a user-level name wins over a checkout's); a
crewmate's worker runs its crewmate's merged spec. The scope is
``acp/kas_skill_scope.scope_globs`` over that spec, and the files it loads are
``kas_skill_scope.loaded_skill_files``, the classifier a KAS start runs.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple


class KasSkillScope(NamedTuple):
    """A KAS session's skills, as the remote workspace copy reads them.

    A verdict is bound to the file KAS's classifier read: its identity
    (``kas_skill_scope.skill_file_id``) and the digest of the bytes classified.
    :meth:`bind` fixes, at selection, the exact file the copy may read; the copy
    checks it on the descriptor and bytes it read, so anything swapped since is
    refused.
    """

    #: The globs to list catalog rows by, or ``None`` for an unmapped
    #: whole-catalog template (Crew's whole catalog).
    globs: list[str] | None
    #: Identity -> digest of the bytes classified, for each file KAS loads.
    loaded: dict[tuple[int, int], str]
    #: The identities of every skill file KAS reaches, loaded or not.
    weighed: set[tuple[int, int]]
    #: Those files' paths, as written and resolved, and the roots KAS reads
    #: under (ending in a separator): ``kas_skill_scope.reaches``.
    paths: set[str]
    #: The session's ``skill`` rules: the spec's own, then the scope's.
    rules: list[dict[str, Any]]

    def _reached(self, skill_file: str, ident: tuple[int, int] | None) -> bool:
        from kiro_crew.acp import kas_skill_scope

        return ident in self.weighed or kas_skill_scope.reaches(self.paths, skill_file)

    def bind(self, skill_file: str, *, crew_only: bool = True) -> dict[str, Any] | None:
        """A catalog row's copy, fixed now from one stat, or ``None`` to leave it out.

        ``real_dir`` is where the copy reads the skill (never resolved again), and
        ``admit`` holds the copy to the exact ``SKILL.md`` judged here: its
        ``(st_dev, st_ino)`` must match the open descriptor's, so a link retargeted
        after this drops the skill (fail closed). A file KAS reaches is kept only
        if KAS loads it, and only with the bytes KAS's classifier read; with
        *crew_only* false every row must be one KAS loads. Any other is judged by
        its names under the session's rules, again on the bytes copied.
        """
        import os

        from kiro_crew.acp import kas_skill_scope

        ident = kas_skill_scope.skill_file_id(skill_file)
        if ident is None:
            return None
        real_dir = os.path.dirname(os.path.realpath(skill_file))
        check: Callable[[str, tuple[int, int], bytes], bool]
        if not crew_only or self._reached(skill_file, ident):
            digest = self.loaded.get(ident)
            if digest is None:
                return None

            def check(_path: str, got: tuple[int, int], data: bytes) -> bool:
                return got == ident and kas_skill_scope.skill_bytes_digest(data) == digest

        else:
            if not kas_skill_scope.crew_only_file_kept(self.rules, skill_file):
                return None
            rules = self.rules

            def check(path: str, got: tuple[int, int], data: bytes) -> bool:
                return got == ident and kas_skill_scope.crew_only_skill_kept(rules, data, path)

        return {"real_dir": real_dir, "admit": check}


def kas_session_skill_scope(
    session_key: str, agent: str, work_dir: str | Path | None
) -> KasSkillScope:
    """The skills a KAS session of *agent* loads. A failure raises.

    ``scope_globs`` answers ``None`` both for an unmapped whole-catalog template
    and for a scope that is exactly the default trees (KAS needs no rule for it).
    Only the first is Crew's whole catalog here; the second lists those trees, so
    a template mapped to them is not read as the whole catalog.
    """
    from kiro_crew import agent as agent_mod
    from kiro_crew.acp import kas_skill_scope
    from kiro_crew.acp.harness.kas import mate_worker_projection, resolve_projected_spec
    from kiro_crew.agent_discovery import (
        WHOLE_CATALOG_TEMPLATES,
        inherited_skill_globs,
        skill_resource_uris,
    )
    from kiro_crew.apps.agent_launch import read_bound_app_spec
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    agents_dir = kiro_agents_dir()
    snapshot = agent_mod.require_fresh_derived_spec(agent, work_dir)
    bound = read_bound_app_spec(session_key, agent)
    if snapshot is not None and snapshot.spec is not None:
        spec, spec_dir = snapshot.spec, agents_dir
    elif bound is not None:
        spec, spec_dir = bound, agents_dir
    else:
        spec, spec_dir = resolve_projected_spec(agents_dir, agent, work_dir)
    if agent == MATE_WORKER_AGENT:
        # An unreadable kit raises, as it refuses the session in KAS.
        spec = mate_worker_projection(spec, spec_dir, session_key)
    globs = kas_skill_scope.scope_globs(agent, spec, agents_dir=spec_dir, work_dir=work_dir)
    loaded, weighed, paths, rules = kas_skill_scope.loaded_skill_files(
        agent, spec, globs, agents_dir=spec_dir, work_dir=work_dir
    )
    if globs is not None:
        return KasSkillScope(globs, loaded, weighed, paths, rules)
    if agent in WHOLE_CATALOG_TEMPLATES and not skill_resource_uris(spec):
        return KasSkillScope(None, loaded, weighed, paths, rules)
    # Any mapping: a template whose skill:// entries all fail to expand is the whole
    # catalog to KAS, and gets these trees here, the narrower of the two.
    return KasSkillScope(inherited_skill_globs(work_dir), loaded, weighed, paths, rules)


def kas_unsafe_to_ship(resolved: str) -> bool:
    """Whether the resolved path must not leave this host, as KAS's prompt reader
    judges it (``acp/kas_prompt_paths.is_unsafe_prompt_path``): the sensitive-path
    fence plus what only a shipped read refuses (``~/.config/gh``, the data home's
    receipt key and session-token mappings)."""
    from kiro_crew.acp.kas_prompt_paths import is_unsafe_prompt_path

    return is_unsafe_prompt_path(Path(resolved))
