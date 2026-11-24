"""The last step of a KAS session's ``customAgents`` batch: who its sub-agent tool may run.

``KasHarness.session_extras`` hands this the session agent's projected entries and
the targets ``kas_subagent_targets.project_subagent_targets`` registered. In a
session ``kas_engine_subagents.engine_subagents_on`` names, the default agent's
entry mounts the engine's sub-agent tool with the shipped helper and the person's
own registered agents reachable.
Then Kiro Crew's own agents are denied to every entry, whether it mounts
``invoke_sub_agent`` or only a per-agent ``subagent_<id>`` tool, listed or not (``kas_subagent_targets.deny_own_agents``), and every engine
sub-agent is denied to an entry whose ``excludedTools`` withhold the tool
(``kas_subagent_targets.deny_excluded_subagent_tool``). The mount's deny then
goes behind those on the mounted entry
(``kas_engine_subagent_target.mount_deny_last``). With the ``kas_subagents``
preview off or never set, no other rule is added: each entry is projected for the
engine's sub-agent tools as it was before the preview existed. On, in the chats
the mount reaches, a person's own agent whose file excludes the tool the way a
file run on kiro-cli did is read as kiro-cli read it
(``kas_excluded_tool_cli.hold_excluded_as_cli``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from kiro_crew.acp import kas_excluded_tool_cli, kas_subagent_targets
from kiro_crew.acp.kas_engine_subagent_target import KAS_HELPER_AGENT, mount_deny_last
from kiro_crew.acp.kas_subagent_targets import deny_own_agents

logger = logging.getLogger(__name__)


def native_batch(
    custom_agents: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    *,
    agent: str = "",
    session_key: str = "",
    member_dispatch: bool = False,
    crew_panel: bool = False,
    agents_dir: Path | None = None,
    work_dir: str | Path | None = None,
    stub_server_names: frozenset[str] = frozenset(),
    settings_dir: str | Path | None = None,
    notices: list[str] | None = None,
    parent_spec: dict[str, Any] | None = None,
    remote_workspace: bool = False,
    mounted_out: list[str] | None = None,
    own_agents: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
    spec_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """The session's batch: its own entries, then its targets, own agents denied.

    Blocking (a config read): runs in the harness's projection thread. A config
    that cannot be read mounts nothing, and neither does a session in a remote
    workspace, whose built-ins (the engine's sub-agent tool among them) are
    replaced by the workspace's own tools.
    *own_agents* projects the person's own agents
    (``project_subagent_targets(every_agent=True)``); only an accepted engine
    mount calls it. *spec_dir* is the folder *parent_spec* was read from.
    """
    batch = [*custom_agents, *targets]
    if agents_dir is not None:
        from kiro_crew.acp import kas_engine_subagents as engine
        from kiro_crew.config.loader import KiroCrewConfig

        try:
            cfg = KiroCrewConfig.load()
        except Exception:  # noqa: BLE001 - an unreadable config keeps Kiro Crew's own
            logger.warning("config unreadable; KAS sub-agents stay off", exc_info=True)
            cfg = None
        if (
            cfg is not None
            and not remote_workspace
            and engine.engine_subagents_on(
                cfg,
                agent,
                session_key,
                member_dispatch=member_dispatch,
                crew_panel=crew_panel,
            )
        ):
            batch = engine.with_engine_subagents(
                batch,
                agent,
                agents_dir=agents_dir,
                work_dir=work_dir,
                stub_server_names=stub_server_names,
                session_key=session_key,
                settings_dir=settings_dir,
                notices=[] if notices is None else notices,
                parent_spec=parent_spec,
                mounted_out=mounted_out,
                own_agents=own_agents,
            )
        # The chats the preview reaches, whatever their agent: the mount's own
        # test, asked as for the default agent.
        reads_as_cli = (
            cfg is not None
            and not remote_workspace
            and agent != engine.DEFAULT_AGENT
            and engine.engine_subagents_on(
                cfg,
                engine.DEFAULT_AGENT,
                session_key,
                member_dispatch=member_dispatch,
                crew_panel=crew_panel,
            )
        )
        if reads_as_cli:
            batch = kas_excluded_tool_cli.hold_excluded_as_cli(
                deny_own_agents(batch),
                agent,
                parent_spec,
                spec_dir=spec_dir,
                agents_dir=agents_dir,
                work_dir=work_dir,
                notices=notices,
            )
            return mount_deny_last(batch, agent)
    batch = kas_subagent_targets.deny_excluded_subagent_tool(deny_own_agents(batch))
    # The engine quotes the first deny that matches: the denies that name agents
    # go before the mount's deny on every name.
    return mount_deny_last(batch, agent)


def name_left_out(
    custom_agents: list[dict[str, Any]], agent: str, names: Iterable[str]
) -> list[dict[str, Any]]:
    """*custom_agents* with *names* denied by name to *agent*'s sub-agent tool.

    *names* are the person's own agents the default agent's mount found and left
    out. The mount's deny on every name already refuses them, and the engine quotes
    only the ``match`` of the first deny that matches a refused call, so the text
    read ``deny subagent matching "*"``, as if every sub-agent were blocked. A deny
    that names them, kept before the mount's
    (``kas_engine_subagent_target.mount_deny_last``), makes it name the agent.
    Which calls are refused does not change. No name, or a batch the mount is not
    in, comes back as it is.
    """
    left_out = sorted(set(names))
    if not left_out or not any(a.get("id") == KAS_HELPER_AGENT for a in custom_agents):
        return custom_agents
    denied = kas_subagent_targets.deny_refused_targets(custom_agents, agent, left_out)
    return mount_deny_last(denied, agent)


__all__ = ["name_left_out", "native_batch"]
