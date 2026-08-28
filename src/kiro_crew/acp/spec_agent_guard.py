"""Agent-profile enforcement for public-ACP-spec backends.

kiro-cli activates a named agent with ``session/set_mode``, so a spec whose
``tools`` list withholds the shell is actually enforced on the wire. A spec
adapter has no ``set_mode`` equivalent: the agent name is not sent, the adapter
runs with its OWN built-in tool set, and a shell-less agent would silently gain
full shell access.

That is a privilege escalation for exactly the agents that matter — restricted
app and subagent agents whose narrowed tool set IS their security boundary — so
the combination is refused rather than downgraded.

Kiro Crew's own global agent files are exempt. Their narrowed tool sets are Kiro
Crew's own scope choice rather than a boundary against Kiro Crew, and refusing
them bricked the background session on the reference implementation's first live
deploy. The exemption is file-owned, not name-owned: a project spec can reuse an
owned name and still carries the project's restriction.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Tool names that grant shell execution. ``*`` grants everything, so it counts.
_SHELL_TOOLS = frozenset({"execute_bash", "shell", "*"})


class SpecAdapterAgentRefused(Exception):
    """The agent cannot be honoured on a backend with no ``set_mode``."""


def _grants_shell(spec: dict) -> bool:
    """Whether a resolved spec's ``tools`` list grants shell execution.

    A spec with NO ``tools`` key does not restrict tools at all, so it grants
    shell by omission — that is kiro-cli's own reading, and treating absence as a
    restriction would refuse most third-party agents for no reason.
    """
    tools = spec.get("tools")
    if tools is None:
        return True
    if not isinstance(tools, list):
        # Malformed: cannot establish that shell is withheld, so do not claim it.
        return True
    return any(isinstance(tool, str) and tool in _SHELL_TOOLS for tool in tools)


def _candidate_paths(agent: str, work_dir: Path | str) -> list[Path]:
    """The spec file discovery resolves for ``agent`` in this workspace.

    Agent identity comes from the JSON ``name`` field, not necessarily the
    filename. Reuse the shared discovery index so project shadowing, package
    filenames, hardened reads, and declared names cannot drift from dispatch.
    """
    from kiro_crew.agent_discovery import SCOPE_PROJECT, _read_agent_spec, list_agents
    from kiro_crew.config.paths import kiro_agents_dir, project_agents_dir

    user_dir = kiro_agents_dir()
    try:
        project_dir = project_agents_dir(work_dir)
    except (OSError, ValueError):
        project_dir = None
    # Preserve fail-closed handling for an exact-name spec that the hardened
    # discovery reader rejects. Such a file is omitted from ``list_agents``, but
    # it is still the path dispatch conventionally associates with the requested
    # name and must not turn an unreadable restriction into a grant.
    exact: list[Path] = []
    if project_dir is not None:
        project_path = project_dir / f"{agent}.json"
        if project_path.is_file():
            exact.append(project_path)
    user_path = user_dir / f"{agent}.json"
    if user_path.is_file():
        exact.append(user_path)
    try:
        matches = [
            info
            for info in list_agents(agents_dir=user_dir, project_dir=work_dir)
            if info.name == agent and info.filename
        ]
    except (OSError, ValueError):
        return exact
    if not matches:
        return exact
    resolved = matches[0]
    if resolved.scope == SCOPE_PROJECT:
        if project_dir is None:
            return exact
        return [project_dir / resolved.filename]
    # The shared discovery index omits unreadable files. That must not make an
    # unreadable exact-name project shadow disappear and expose a permissive
    # global spec that kiro-cli would not necessarily be able to activate.
    if project_dir is not None:
        project_path = project_dir / f"{agent}.json"
        if project_path in exact and _read_agent_spec(project_path) is None:
            return [project_path]
    return [user_dir / resolved.filename]


def shell_restriction(agent: str, work_dir: Path | str) -> str:
    """Describe why ``agent`` cannot run on a spec adapter, or ``""``.

    ``""`` means "no positive finding": the resolved spec is a global file Kiro
    Crew owns, there is no spec on disk, or the spec grants shell. Only a spec
    that demonstrably withholds shell produces a refusal reason, so a host with
    no third-party agents is never blocked by this.

    A spec Kiro Crew's own reader refuses (over the size cap, a symlink resolving
    somewhere sensitive) yields "unverifiable" rather than "fine". Treating
    unreadable as permissive would let an attacker bypass the check by making the
    spec unreadable.
    """
    if not agent:
        return ""

    from kiro_crew.agent_discovery import _read_agent_spec
    from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES
    from kiro_crew.config.paths import kiro_agents_dir

    for path in _candidate_paths(agent, work_dir):
        # Owned filenames are exempt only in Crew's global directory. A project
        # can reuse the basename but Crew neither wrote nor controls that file.
        if path.parent == kiro_agents_dir() and path.name in OWNED_KIRO_AGENT_FILES:
            continue
        spec = _read_agent_spec(path)
        if spec is None:
            return (
                f"its spec at {path} could not be read, so whether it withholds "
                "shell access cannot be established"
            )
        if not _grants_shell(spec):
            return f"its spec at {path} withholds shell access (tools=" f"{spec.get('tools')!r})"
    return ""


def assert_agent_permitted(agent: str, backend_label: str, work_dir: Path | str) -> None:
    """Refuse an agent whose restriction the backend cannot enforce."""
    restriction = shell_restriction(agent, work_dir)
    if not restriction:
        return
    raise SpecAdapterAgentRefused(
        f"Agent {agent!r} cannot be activated on the {backend_label} backend: "
        f"{restriction}, but this backend has no session/set_mode equivalent to "
        "enforce it — running the session here would silently grant full shell "
        "access. Clear agent.acp_backend to use the default kiro-cli backend for "
        "this agent, or run it as the default kirocrew agent."
    )


__all__ = [
    "SpecAdapterAgentRefused",
    "assert_agent_permitted",
    "shell_restriction",
]
