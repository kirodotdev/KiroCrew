"""Shared source-selection rules for KAS projections and their companion readers.

A KAS session runs the spec Crew hands it, so every reader that serves that
session (the projection, the broker overlay and its subtraction, the spec hooks)
must take the same file. Two rules decide it, in this order:

* a session bound to an app's own agent (``apps.agent_launch``) runs that app's
  registered file, read by its path (:func:`kas_bound_app_spec`,
  :func:`kas_bound_source_stem`);
* otherwise an ordinary user-level agent runs the newest of its claimants
  (:func:`kas_projects_newest`, :func:`load_kas_agent_spec`).

Lives in the SDK because application readers (the broker overlay, the hook
gate) ask it and it reaches the ACP layer, at call time only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kiro_crew.agent_files import is_mate_service_agent
from kiro_crew.config import paths


def _is_derived_agent(agent_id: str) -> bool:
    # Import at call time: materialization imports agent, which loads the SDK.
    from kiro_crew import agent

    return agent_id in agent.worker_agent._DERIVED_AGENTS


def kas_projects_newest(agent_id: str | None, *, agents_dir: Path | None = None) -> bool:
    """Whether KAS resolves *agent_id* through the newest user-level claimant.

    The freshness gate owns derived agents' canonical files. Mate service agents
    use the crewmate folder. Other crewmate and project-only agents have no user
    spec and never opt in. A user-level declaration still outranks those scopes.
    This is a scope decision, not a freshness or governance check. A session
    bound to an app's agent is decided before this (:func:`kas_bound_app_spec`).
    """
    from kiro_crew.acp.kas_agents import agent_spec_absent

    if not agent_id or is_mate_service_agent(agent_id) or _is_derived_agent(agent_id):
        return False
    user_dir = paths.kiro_agents_dir()
    if agents_dir is not None and agents_dir != user_dir:
        return False
    return not agent_spec_absent(user_dir, agent_id)


def kas_bound_source_stem(session_key: str, agent_id: str) -> str | None:
    """The stem of the registered file a KAS session of *agent_id* is bound to.

    ``None`` when *session_key* binds no app's agent of that name. No I/O: a
    reader keyed by the source stem (the broker overlay) uses it to name the
    file the projection reads, never another claimant of the name.
    """
    from kiro_crew.apps.agent_launch import bound_app_spec_path

    path = bound_app_spec_path(session_key, agent_id)
    return None if path is None else path.stem


def kas_bound_app_spec(session_key: str, agent_id: str) -> dict[str, Any] | None:
    """The registered spec a KAS session of *agent_id* is bound to, or ``None``.

    The file the projection reads (``acp.harness.kas`` through
    ``apps.agent_launch.read_bound_app_spec``). Raises
    ``KasAgentTranslationError`` when the bound file cannot be read, so a reader
    that gates on it fails closed instead of reading another claimant.
    """
    from kiro_crew.acp.kas_agents import KasAgentTranslationError
    from kiro_crew.apps.agent_launch import AppAgentLaunchError, read_bound_app_spec

    try:
        return read_bound_app_spec(session_key, agent_id)
    except AppAgentLaunchError as exc:
        raise KasAgentTranslationError(exc.detail) from exc


def load_kas_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read the source KAS selects, without materializing or verifying a mirror.

    Session start takes derived bytes from ``require_fresh_derived_spec`` itself.
    Companion readers inspect that same canonical path, never another claimant;
    this read must not repair files merely to inspect hooks or a load error.
    """
    from kiro_crew.acp.kas_agents import KasAgentTranslationError, load_agent_spec
    from kiro_crew.agent_discovery import read_agent_spec_strict

    newest = kas_projects_newest(agent_id, agents_dir=agents_dir)
    if not newest and _is_derived_agent(agent_id):
        path = agents_dir / f"{agent_id}.json"
        try:
            spec = read_agent_spec_strict(path, operation="kas_agent_projection", source="unknown")
        except (OSError, ValueError) as exc:
            raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
        return spec
    return load_agent_spec(agents_dir, agent_id, prefer_newest=newest)
