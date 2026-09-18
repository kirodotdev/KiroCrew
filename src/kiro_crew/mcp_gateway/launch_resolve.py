"""Resolve the launch a stubbed MCP server name runs as, at the operator's decision.

The approval of a stubbed server is bound to the launch it runs
(:mod:`kiro_crew.mcp_gateway.launch_approval`). That launch has to be read at
the moment the operator decides -- the dashboard toggle, or seeding -- because
the rewrite that would otherwise observe it runs only at the next gateway
start, and an agent can edit ``~/.kiro/crew/mcp.json`` or an agent spec in
between.

Resolution replays the gateway start. It first rebuilds the agent config the
way boot does (``agent.rebuild_agent_config``, which merges
``~/.kiro/crew/mcp.json`` and ``~/.kiro/settings/mcp.json`` into the spec), then
runs the rewriter's own pass against the same inputs the gateway start hands it
(:func:`rewrite_kwargs`), into a throwaway overlay directory and with a probe
:class:`~kiro_crew.mcp_gateway.launch_approval.LaunchApprovals`. So the command,
args and env recorded here are the ones the real pass will hash, with no second
copy of either merge.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from kiro_crew.mcp_gateway.launch_approval import LaunchApprovals, ResolvedLaunch, target_stem

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)


class ResolvedLaunches(dict[str, list[ResolvedLaunch]]):
    """``{name: launches}`` plus the names whose launch set exceeded a cap.

    A name in :attr:`over_cap` has no entry: approving the launches that fit
    would approve a partial set the operator never saw whole.
    """

    def __init__(self, *args: Any, over_cap: Iterable[str] = (), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.over_cap: frozenset[str] = frozenset(over_cap)


def rewrite_kwargs(cfg: KiroCrewConfig, stub_servers: frozenset[str]) -> dict[str, Any]:
    """The ``rewrite_agents`` inputs the gateway start derives from *cfg*.

    One definition, used by the gateway's own rewrite and by
    :func:`resolve_launches`, so the launch approved at a decision is resolved
    from exactly what the rewrite will read.
    """
    # Deferred: ``config.loader`` imports the rewriter's path helpers.
    from kiro_crew.config.loader import session_default_cwd
    from kiro_crew.config.paths import kiro_agents_dir
    from kiro_crew.mcp_gateway.rewriter import default_socket_path, resolve_overlay_dir

    cfg_gw = cfg.mcp_gateway
    return {
        "source_dir": kiro_agents_dir(),
        "overlay_dir": resolve_overlay_dir(cfg_gw.overlay_dir),
        "socket_path": Path(cfg_gw.socket_path) if cfg_gw.socket_path else default_socket_path(),
        "work_dir": session_default_cwd(None),
        "sandbox_mode": cfg.agent.sandbox,
        "approval_mode": cfg.agent.approval_mode,
        "stub_servers": stub_servers,
        "pooling_enabled": cfg_gw.enabled,
    }


def _refresh_agent_specs() -> None:
    """Bring the agent specs up to date with their sources, as boot does.

    Best-effort: a failed rebuild leaves the previous specs, and a launch
    resolved from those that the next boot's rebuild changes is refused then.
    """
    # Deferred: ``agent`` imports the gateway package.
    from kiro_crew.agent import rebuild_agent_config

    try:
        rebuild_agent_config()
    except Exception:
        logger.warning(
            "mcp launch resolution: agent config rebuild failed; resolving from "
            "the current specs",
            exc_info=True,
        )


def resolve_launches(
    names: Iterable[str],
    *,
    cfg: KiroCrewConfig | None = None,
    refresh_specs: bool = True,
) -> ResolvedLaunches:
    """``{name: launches}`` for every one of *names* the rewriter would wrap now.

    *names* are resolved as stubbed, alongside the servers already stubbed in
    *cfg* (loaded when omitted). A name absent from the result resolved to no
    launch the gateway would run -- not declared by any agent, not a stdio
    server, or a command that does not resolve -- so there is nothing to
    approve for it. A name whose launches exceed the approval store's caps is
    listed in ``over_cap`` instead. ``refresh_specs`` rebuilds the agent config first, as the
    gateway start does before its rewrite. BLOCKING: a rebuild and a full
    rewrite pass.
    """
    from kiro_crew.mcp_gateway.backend_tmp import allocate_probe_tmp
    from kiro_crew.mcp_gateway.rewriter import rewrite_agents

    wanted = {target_stem(n): n for n in names if isinstance(n, str) and n}
    if not wanted:
        return ResolvedLaunches()
    if refresh_specs:
        _refresh_agent_specs()
    if cfg is None:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
    stubs = frozenset(cfg.mcp_gateway.stub_servers) | frozenset(wanted.values())
    kwargs = rewrite_kwargs(cfg, stubs)
    # Keep the unsandboxed rewriter under the managed, owner-only probe root;
    # the agent-writable overlay parent cannot plant a path for this pass to follow.
    scratch = allocate_probe_tmp()
    probe = LaunchApprovals(probe=True)
    try:
        rewrite_agents(**{**kwargs, "overlay_dir": scratch / "agents"}, approvals=probe)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    over_cap = {name for stem, name in wanted.items() if probe.over_cap(stem)}
    resolved = ResolvedLaunches(over_cap=over_cap)
    for stem, name in wanted.items():
        launches = probe.captured_launches.get(stem)
        if launches and name not in over_cap:
            resolved[name] = [launches[fp] for fp in sorted(launches)]
    return resolved
