"""A remote session's workspace: built-in file and shell tools give way to its server.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_remote_workspace``.

Instance fields read: ``_process``, ``check_remote_workspace_supported``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


def _restricts_replaced_builtins(permissions: Any) -> bool:
    """Whether a KAS ``permissions`` block limits a capability the workspace replaces.

    A ``deny``, or a scoped ``ask``, on ``shell``/``fs_read``/``fs_write`` (or a
    meta capability covering one). An unscoped ``ask`` is Crew's own hook hold,
    which the gate keeps answering for workspace tools.
    """
    from kiro_crew.acp.kas_permissions import META_CAPABILITY_EXPANSION

    rules = permissions.get("rules") if isinstance(permissions, dict) else None
    for rule in rules if isinstance(rules, list) else ():
        if not isinstance(rule, dict) or not isinstance(rule.get("capability"), str):
            continue
        covered = META_CAPABILITY_EXPANSION.get(rule["capability"], (rule["capability"],))
        if not {"shell", "fs_read", "fs_write"} & set(covered):
            continue
        scoped = bool(rule.get("match") or rule.get("exclude"))
        if rule.get("effect") == "deny" or (rule.get("effect") == "ask" and scoped):
            return True
    return False


def _without_local_only_grants(grants: list[str]) -> list[str]:
    """MCP grants for a remote session, minus every tool that acts on this machine.

    The allowlist has no per-tool exclusion, so a whole ``@kirocrew-core`` grant
    becomes one grant per non-local-only tool, named from the titles table that
    ``test_mcp_tool_titles`` pins. Grants on a local-only server are dropped.
    """
    from kiro_crew.mcp_tool_titles import _table
    from kiro_crew.session_directive import CORE_MCP_SERVER
    from kiro_crew.session_environment import LOCAL_ONLY_CORE_TOOLS, LOCAL_ONLY_SERVERS

    core_tools = sorted(set(_table().get(CORE_MCP_SERVER, {})) - LOCAL_ONLY_CORE_TOOLS)
    out: list[str] = []
    for grant in grants:
        server, _, tool = grant[1:].partition("/")
        if server in LOCAL_ONLY_SERVERS:
            continue
        if server == CORE_MCP_SERVER:
            if not tool:
                out.extend(f"@{CORE_MCP_SERVER}/{t}" for t in core_tools)
                continue
            if tool in LOCAL_ONLY_CORE_TOOLS:
                continue
            if "*" in tool or "?" in tool:
                from kiro_crew.acp.kas_permissions import _mcp_pattern

                if _mcp_pattern(grant) is not None:
                    out.extend(
                        f"@{CORE_MCP_SERVER}/{t}"
                        for t in core_tools
                        if fnmatch.fnmatchcase(t, tool)
                    )
                continue
        out.append(grant)
    return list(dict.fromkeys(out))


#: Workspace tools standing in for each built-in, by kiro-cli name: only a write
#: grant reaches writes, only a shell grant ``run_command``.
#: ``test_remote_session_launch`` pins these to the guest server's tools.
_WORKSPACE_TOOLS_BY_BUILTIN: dict[str, tuple[str, ...]] = {
    "fs_read": ("read_file", "list_directory"),
    "glob": ("glob",),
    "grep": ("grep",),
    "fs_write": ("write_file", "edit_file"),
    "execute_bash": ("run_command",),
}
#: The same, for an allowlist that spells KAS's own tool ids: each id becomes the
#: one workspace tool that does what it does, so a grant keeps its scope.
#: ``fs_append`` and ``delete_file`` have no workspace tool that does only that,
#: and get none rather than a wider one.
_WORKSPACE_TOOLS_BY_KAS_TOOL: dict[str, tuple[str, ...]] = {
    "read_file": ("read_file",),
    "list_directory": ("list_directory",),
    "file_search": ("glob",),
    "grep_search": ("grep",),
    "str_replace": ("edit_file",),
    "execute_pwsh": ("run_command",),
    "run_command": ("run_command",),
}


def _workspace_grants(server: str, grants: list[str]) -> list[str]:
    """The ``@<server>`` grants giving a remote agent what its built-ins gave it.

    ``@builtin`` becomes the whole server; each named built-in its workspace
    tools (none for ``web_fetch``); a set covering every tool, the whole server.
    """
    from kiro_crew.acp.kas_permissions import KIRO_TOOL_ALIASES

    tools: list[str] = []
    for grant in grants:
        if grant == "@builtin":
            return [f"@{server}"]
        if grant.startswith("@builtin/"):
            grant = grant[len("@builtin/") :]
        elif grant.startswith("@"):
            continue
        name = KIRO_TOOL_ALIASES.get(grant, grant)
        replacements = _WORKSPACE_TOOLS_BY_BUILTIN.get(name) or _WORKSPACE_TOOLS_BY_KAS_TOOL.get(
            grant, ()
        )
        for tool in replacements:
            if tool not in tools:
                tools.append(tool)
    every = {t for row in _WORKSPACE_TOOLS_BY_BUILTIN.values() for t in row}
    if tools and set(tools) == every:
        return [f"@{server}"]
    return [f"@{server}/{tool}" for tool in tools]


async def _close_remote_workspace(self: AcpRuntime) -> None:
    """Await the remote workspace's close: a command its guest worker still runs
    must have ended before whoever killed this runtime starts its replacement.

    Read with a default, as the kill path reads ``_bound_workspace_fd``: a runtime
    assembled without ``__init__`` never had a remote workspace to close."""
    workspace = getattr(self, "_remote_workspace", None)
    close = workspace.get("close") if isinstance(workspace, dict) else None
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 -- the relay's own end-watcher retries
        logger.warning("Could not close this runtime's remote workspace", exc_info=True)


def set_remote_workspace(self: AcpRuntime, workspace: Any) -> None:
    """Give this runtime's sessions a remote workspace instead of local files.

    ``workspace`` is ``{"server": <mcpServers element>, "workdir": <guest path>}``.
    The engine, Crew servers, credentials and approvals stay here; built-in
    file and shell tools are unmounted for the workspace server. That fence
    needs a host that sends the allowlist over the wire: KAS (positive identity).
    """
    self.check_remote_workspace_supported()
    self._remote_workspace = workspace


def check_remote_workspace_supported(self: AcpRuntime) -> None:
    """Raise before any guest compute is touched when this runtime cannot fence it."""
    from kiro_crew.session_environment import REMOTE_NEEDS_KAS

    if self._process is not None:
        raise AcpRuntimeError("a remote workspace must be set before the runtime spawns")
    if self.acp_backend == ACP_BACKEND_KAS:
        return
    raise AcpRuntimeError(REMOTE_NEEDS_KAS)


async def _write_remote_token(self: AcpRuntime) -> None:
    """Put the relay token in this runtime's private scratch and point the bridge at it."""
    workspace = self._remote_workspace
    if workspace is None or "write_token" not in workspace:
        return
    scratch = self.work_scratch_dir
    if scratch is None:
        raise AcpRuntimeError("a remote workspace needs this session's private scratch")
    path = await asyncio.to_thread(workspace["write_token"], scratch)
    server = workspace["server"]
    args = list(server["args"])
    args[args.index("--token-file") + 1] = str(path)
    workspace["server"] = {**server, "args": args}


def _remote_session_servers(self: AcpRuntime, servers: list[Any]) -> list[Any]:
    """The session's MCP array: unchanged, plus the remote workspace server."""
    if self._remote_workspace is None:
        return servers
    from kiro_crew.session_environment import LOCAL_ONLY_SERVERS

    entry = self._remote_workspace["server"]
    # A local-only server (computer use, cron) is not sent at all.
    kept = [
        s
        for s in servers
        if s.get("name") != entry["name"] and s.get("name") not in LOCAL_ONLY_SERVERS
    ]
    return kept + [entry]


def _remote_workspace_agents(self: AcpRuntime, agents: Any, servers: list[Any]) -> Any:
    """Agent definitions with built-in tools unmounted for a remote workspace.

    MCP grants survive; ``"*"`` becomes the servers sent. Every built-in acts
    on this machine, so each is replaced by its workspace tools
    (:func:`_workspace_grants`); an empty allowlist stays empty.
    """
    if self._remote_workspace is None or not agents:
        return agents
    name = self._remote_workspace["server"]["name"]
    workdir = self._remote_workspace["workdir"]
    notice = (
        f"\n\nThis session's files and shell live in a remote workspace at {workdir}. "
        f"Use the @{name} tools for every file and command; local file and shell "
        "tools are not available. Git over HTTPS or SSH to GitHub, GitLab, Bitbucket "
        "and Azure DevOps (dev.azure.com) works from those commands as usual. For a "
        "host the gateway has a git login for and is allowed to lend, git goes "
        "through the gateway, which adds that login; any other host uses the "
        "workspace's own git login, if it has one. Under the strict agent sandbox "
        "the gateway lends none, so only the workspace's own login applies. A push "
        "through the gateway that updates or deletes a protected branch such as main "
        "is refused whole, as it is for a local session; push a feature branch "
        "instead. Do not go looking for keys or tokens, and do not unset git's url "
        "rewrites."
    )
    out = []
    for agent in agents:
        # KAS judges workspace tools as `mcp`, so the agent's own shell and
        # file restrictions would stop matching: refuse instead of dropping them.
        if _restricts_replaced_builtins(agent.get("permissions")):
            from kiro_crew.session_environment import REMOTE_AGENT_RESTRICTED

            raise AcpRuntimeError(REMOTE_AGENT_RESTRICTED)
        original = agent
        agent = dict(agent)
        tools = agent.get("tools")
        if tools == "*" or (isinstance(tools, list) and "*" in tools):
            kept = [f"@{s['name']}" for s in servers if s.get("name")]
        else:
            # ``@builtin`` is the built-in tool set spelled like a server grant.
            grants = [t for t in tools or [] if isinstance(t, str)]
            kept = [t for t in grants if t.startswith("@") and not t.startswith("@builtin")]
            for grant in _workspace_grants(name, grants):
                if grant not in kept:
                    kept.append(grant)
        agent["tools"] = _without_local_only_grants(kept)
        # The tool_search loader runs here and loads deferred MCP specs, the
        # workspace server's included; the handshake already enabled deferral from
        # the original grant, so the loader stays mounted whenever it was granted.
        from kiro_crew.agent_sdk.tool_search import (
            TOOL_SEARCH_LOADER_TOOL,
            spec_grants_tool_search,
        )

        if spec_grants_tool_search(original) and TOOL_SEARCH_LOADER_TOOL not in agent["tools"]:
            agent["tools"].append(TOOL_SEARCH_LOADER_TOOL)
        # A local-only server the agent declares in its own block (one the session-level
        # hoist left there) is not sent either: it would act on this machine, and the
        # managed-server readiness barrier would wait for a server the fence withholds.
        block = agent.get("mcpServers")
        if isinstance(block, dict):
            from kiro_crew.session_environment import LOCAL_ONLY_SERVERS

            agent["mcpServers"] = {k: v for k, v in block.items() if k not in LOCAL_ONLY_SERVERS}
        excluded = agent.get("excludedTools")
        if isinstance(excluded, list):
            from kiro_crew.platform.tool_names import KIRO_CLI_NAME_BY_KAS_TOOL

            entries = [t for t in excluded if isinstance(t, str)]
            # A KAS id with its own workspace tool excludes just that tool; one with
            # none (``fs_append``, ``delete_file``) excludes its whole write family.
            replacements = _workspace_grants(
                name,
                [
                    t if t in _WORKSPACE_TOOLS_BY_KAS_TOOL else KIRO_CLI_NAME_BY_KAS_TOOL.get(t, t)
                    for t in entries
                ],
            )
            agent["excludedTools"] = list(dict.fromkeys([*entries, *replacements]))
        agent["prompt"] = str(agent.get("prompt") or "") + notice
        out.append(agent)
    return out
