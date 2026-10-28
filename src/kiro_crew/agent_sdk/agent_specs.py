"""Read a Crew agent spec from application code without importing the ACP layer.

The crewmate modules (``mate_config``, ``mate_worker``, ``mate_worker_kit``,
``mate_capability_catalog``, ``mate_config_migration``, and the heartbeat's ``goal_reminder``) read
custom-agent specs the way a session would: the hardened reader in
``acp.kas_agents`` and the per-session MCP switch-off in ``acp.session_mcp``.
Application code may not import ``kiro_crew.acp``
(``scripts/check_agent_sdk_boundary.py``), so this module is the one seam they
reach those reads through.

It translates rather than re-exports: a spec the reader refuses raises
:class:`AgentSpecUnreadable` here, not the ACP layer's
``KasAgentTranslationError``, so a consumer holds no ACP class. Every ACP import
is made at call time, both because ``kiro_crew.acp`` imports back into
``agent_sdk`` at load time and so that a test patching the ACP function on its
own module is seen here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class AgentSpecUnreadable(ValueError):
    """An agent spec exists in a form the reader refuses, or is missing.

    The message is the reader's own, so a caller can quote it unchanged.
    """


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """*agent_id*'s spec in *agents_dir*, as a copy the caller owns. Blocking.

    The same read a KAS session runs (``acp.kas_agents.load_agent_spec``: a spec
    declaring ``name == agent_id`` wins over the file named for it). Raises
    :class:`AgentSpecUnreadable` when there is no readable spec.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.load_agent_spec(agents_dir, agent_id)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def resolve_prompt(spec: dict[str, Any], *, agent_id: str, agents_dir: Path) -> str:
    """*spec*'s prompt as literal text, reading a ``file://`` prompt. Blocking.

    ``acp.kas_agents.resolve_prompt``: a relative path is anchored to
    *agents_dir*, and a prompt-less spec answers the KAS fallback text. Raises
    :class:`AgentSpecUnreadable` when the prompt cannot be read.
    """
    from kiro_crew.acp import kas_agents

    try:
        return kas_agents.resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    except kas_agents.KasAgentTranslationError as exc:
        raise AgentSpecUnreadable(str(exc)) from exc


def agent_spec_absent(agents_dir: Path, agent_id: str) -> bool:
    """Whether *agents_dir* holds no spec at all for *agent_id*. Blocking.

    A spec that exists but cannot be read is NOT absent. Raises ``OSError`` when
    the directory cannot be scanned (``acp.kas_agents.agent_spec_absent``).
    """
    from kiro_crew.acp import kas_agents

    return kas_agents.agent_spec_absent(agents_dir, agent_id)


def session_mcp_server_is_disabled(name: str, agent: str | None) -> bool:
    """Whether MCP server *name* is switched off whole for a session as *agent*.

    ``acp.session_mcp.session_mcp_server_is_disabled``: blocking, and never
    raises (an unreadable source switches nothing off).
    """
    from kiro_crew.acp import session_mcp

    return session_mcp.session_mcp_server_is_disabled(name, agent)


def authored_spec_is_toolless(agent: str, work_dir: Any = None) -> bool:
    """Whether the spec *agent* resolves to in *work_dir* mounts nothing. Blocking.

    ``acp.client_mcp_array.authored_agent_spec_is_toolless``: project level
    first, then user level, judged by ``spec_is_toolless``. Fails closed.
    """
    from kiro_crew.acp import client_mcp_array

    return client_mcp_array.authored_agent_spec_is_toolless(agent, work_dir)


def authored_spec_toolless_verdict(agent: str, work_dir: Any = None) -> bool | None:
    """Like :func:`authored_spec_is_toolless`, but ``None`` when no spec exists. Blocking.

    ``False`` means a spec exists and it mounts something, or it exists and the
    reader will not answer for it: two specs declaring the name (ambiguous), a
    resolved spec the hardened reader refuses, or an ``<agent>.json`` /
    ``<agent>.md`` that is hardlinked, symlinked, oversized or unparsable. Each is
    still something the harness may load, so a caller refuses it before starting
    a session. ``None`` is kept for no file claiming the name, by filename or by
    declared name, in the user-level or project agents directory; that is left to
    the caller's post-start confirmation.
    """
    import os

    from kiro_crew.acp import client_mcp_array, session_mcp
    from kiro_crew.agent import agent_spec_path, kiro_agents_dir_path
    from kiro_crew.agent_spec_format import agent_spec_candidates
    from kiro_crew.config.paths import project_agents_dir

    spec = session_mcp.agent_spec_snapshot(agent, work_dir=work_dir)
    if spec is not None:
        return client_mcp_array.spec_is_toolless(spec)
    try:
        if agent_spec_path(agent) is not None:
            return False
    except ValueError:
        return False
    if session_mcp._project_spec_path_for(agent, work_dir) is not None:
        return False
    dirs = [kiro_agents_dir_path()]
    if work_dir:
        dirs.append(project_agents_dir(work_dir))
    present = any(
        os.path.lexists(candidate)
        for directory in dirs
        for candidate in agent_spec_candidates(directory, agent)
    )
    return False if present else None


def mirrored_session_spec(
    project: str, project_spec: dict[str, Any], user_spec: dict[str, Any] | None
) -> dict[str, Any]:
    """The spec a mirrored session consumes when a checkout spec and a user spec share a name.

    ``acp.session_mcp._agent_spec_and_snapshot_for(mirrored_session=True)``'s
    rule, applied to documents already read: the checkout is trusted to choose
    MCP servers only when ``_project_mcp_trusted`` says so, and then the checkout
    spec is consumed whole. Otherwise the USER spec is consumed with the
    checkout spec's switch-off keys layered on (``_project_restrictions`` /
    ``_with_restrictions``), and the checkout spec reduced to those keys when
    there is no user spec. The stale-config fingerprint compares the document
    this returns, so what the badge watches cannot drift from what the session
    runs on.
    """
    from kiro_crew.acp import session_mcp

    if session_mcp._project_mcp_trusted(project):
        return project_spec
    restrictions = session_mcp._project_restrictions(project_spec)
    if user_spec is not None:
        return session_mcp._with_restrictions(user_spec, restrictions)
    return {**project_spec, "mcpServers": restrictions}


def kas_project_spec_view(project_spec: dict[str, Any]) -> dict[str, Any]:
    """The checkout spec KAS is handed when the user level does not declare the agent.

    ``acp.harness.kas.resolve_projected_spec``'s fallback: the checkout's spec
    reduced to :data:`~kiro_crew.acp.harness.kas.PROJECT_SPEC_HONOURED_KEYS` and
    nothing else, so an edit to a key KAS ignores there is not a change to what
    the session runs on.
    """
    from kiro_crew.acp.harness.kas import PROJECT_SPEC_HONOURED_KEYS

    return {key: project_spec[key] for key in PROJECT_SPEC_HONOURED_KEYS if key in project_spec}
