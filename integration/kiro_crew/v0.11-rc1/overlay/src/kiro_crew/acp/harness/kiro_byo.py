"""Kiro BYO shared-process ACP harness for KiroCrew 0.11.

This file is installed as a separate harness module on purpose: KiroCrew keeps
one adapter per host, and Kiro BYO follows that owner-module boundary rather
than injecting a second implementation into ``harness/__init__.py``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from kiro_crew import agent as agent_mod
from kiro_crew.acp import kas_agents, kas_permissions, launch
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.kas import mate_worker_projection, resolve_projected_spec
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO_BYO,
    ACP_CLIENT_CAPABILITIES,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.config import paths
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.mate_worker import MATE_WORKER_AGENT, WorkerKitUnreadable

__all__ = ["KiroByoHarness"]


def _project_allowed_tools(
    spec: dict[str, object],
    agent_id: str,
    *,
    extra_grants: tuple[str, ...] = (),
) -> list[str] | None:
    """Return the active agent's *safe* no-prompt grants for Kiro BYO.

    ``allowedTools`` is not merely a visibility list: a matching entry means the
    harness emits no ``session/request_permission`` request at all.  Crew controls
    that live on the permission path must therefore be applied BEFORE a grant is
    handed to Kiro BYO.  This follows the same fail-closed projection policy as
    KAS: every entry must survive the governance ceiling, shell/filesystem grants
    that would bypass Crew's deny/sensitive-path floor stay prompting, and a
    PreToolUse hook withholds the covered capability so the call returns to Crew.

    ``None`` preserves the distinction between an agent that never authored an
    allowlist and one that explicitly authored an empty list.
    """
    authored = spec.get("allowedTools")
    if not isinstance(authored, list) and not extra_grants:
        return None
    raw = list(authored) if isinstance(authored, list) else []
    raw.extend(grant for grant in extra_grants if grant not in raw)

    # Kiro BYO is a backend on which Crew fires saved-spec hooks.  A no-prompt
    # grant would otherwise skip those hooks entirely, so determine the hook
    # coverage from the same verified spec snapshot used for this projection.
    hook_matchers = kas_agents.pre_tool_hook_matchers(agent_id, spec)
    projected: list[str] = []
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            continue
        entry = value.strip()

        # The governance ceiling is authoritative at SESSION START, not merely
        # when the agent file was last written.  Stale/hand-edited grants must
        # not outlive a policy change.  may_skip_gate_now fails closed.
        if not may_skip_gate_now(entry):
            continue

        # Reuse Crew's own allowedTools classification rather than maintaining a
        # second security vocabulary in this third-party adapter.  In particular
        # it intentionally withholds unscoped shell/filesystem grants because a
        # skipped permission request would bypass Crew's deny and sensitive-path
        # floor.  Unknown entries likewise remain prompt-only.
        policy = kas_permissions.allowed_tools_to_permissions([entry], agent_id=agent_id)
        policy = kas_permissions.withhold_hook_gated_auto_approval(
            policy,
            hook_matchers,
            agent_id=agent_id,
        )
        if not kas_permissions.auto_approved_capabilities(policy):
            continue
        projected.append(entry)

    return projected


class KiroByoHarness(MembershipHarness):
    """API-native multi-session ACP host.

    Crew owns orchestration, saved-agent projection and approvals. Kiro BYO owns
    provider transport, its durable session record, and the local tool executor.
    """

    backend = ACP_BACKEND_KIRO_BYO

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        _binary, argv, spawn_label, stderr_label = await launch.resolve_self_served_launch(
            self.backend
        )
        return SpawnPlan(argv=argv, spawn_label=spawn_label, stderr_label=stderr_label)

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        env["KIRO_BYO_HOME"] = str(app_data_dir("kiro-byo"))

    @property
    def verifies_agent_activation(self) -> bool:
        # The projection is carried on session/new/load and acknowledged as the
        # active mode. There is no spawn-time --agent flag to re-verify.
        return False

    @property
    def protocol_version(self) -> object:
        return 1

    @property
    def client_capabilities(self) -> dict[str, object]:
        return ACP_CLIENT_CAPABILITIES

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: object = None,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
    ) -> SessionExtras:
        if not agent:
            return SessionExtras()

        def _build() -> tuple[list[dict[str, object]], object]:
            agent_mod.require_fork_governance(agent, work_dir)
            try:
                agent_mod.ensure_agent_materialized(agent)
            except Exception:
                pass
            try:
                snapshot = agent_mod.require_fresh_derived_spec(agent, work_dir)
            except agent_mod.DerivedSpecStale as exc:
                from kiro_crew.acp.session_handle import AcpRuntimeError

                raise AcpRuntimeError(str(exc)) from exc
            agents_dir = paths.kiro_agents_dir()
            if snapshot is not None and snapshot.spec is not None:
                spec, prompt_dir = snapshot.spec, agents_dir
            else:
                # Crew 0.11 Mate 2.0 keeps conductor/worker and mate-owned specs
                # outside the ordinary user agents directory.  Reuse Crew's
                # source-defined resolver so BYO sees the same governed spec as
                # KAS instead of silently widening to a default agent.
                spec, prompt_dir = resolve_projected_spec(agents_dir, agent, work_dir)
            if agent == MATE_WORKER_AGENT:
                try:
                    spec = mate_worker_projection(spec, prompt_dir, session_key)
                except WorkerKitUnreadable as exc:
                    from kiro_crew.acp.session_handle import AcpAgentUnavailable
                    raise AcpAgentUnavailable(str(exc), agent=agent) from exc
            prompt = kas_agents.resolve_prompt(spec, agent_id=agent, agents_dir=prompt_dir)
            raw_tools = spec.get("tools")
            if raw_tools == "*":
                tools: object = ["*"]
            elif isinstance(raw_tools, list):
                tools = [t for t in raw_tools if isinstance(t, str) and t]
            else:
                tools = None
            # AcpRuntime injects the member servers in the session-level MCP
            # array. If this agent explicitly narrows tools, name those servers
            # exactly as the official KAS projection does. An absent tool list is
            # already unrestricted in Kiro BYO, while ["*"] already covers them.
            if isinstance(tools, list) and tools != ["*"]:
                if member_dispatch and "@kirocrew-dashboard" not in tools:
                    tools.append("@kirocrew-dashboard")
                if crew_panel and "@kirocrew-panel" not in tools:
                    tools.append("@kirocrew-panel")

            descriptor: dict[str, object] = {"id": agent, "prompt": prompt}
            if tools is not None:
                descriptor["tools"] = tools
            excluded = spec.get("excludedTools")
            if isinstance(excluded, list):
                descriptor["excludedTools"] = [t for t in excluded if isinstance(t, str) and t]

            # The approval-free member grants are Crew-owned and are filtered by
            # the same governance/hook ceiling as authored allowedTools before
            # Kiro BYO ever sees them. No provider/model detail crosses this seam.
            extra_grants: list[str] = []
            if member_dispatch:
                extra_grants.extend(agent_mod._MEMBER_DASHBOARD_GRANTS)
            if crew_panel:
                extra_grants.extend(
                    grant for grant in agent_mod._MEMBER_PANEL_GRANTS if grant not in extra_grants
                )
            allowed = _project_allowed_tools(spec, agent, extra_grants=tuple(extra_grants))
            if allowed is not None:
                descriptor["allowedTools"] = allowed
            return [descriptor], snapshot

        agents, snapshot = await asyncio.to_thread(_build)
        return SessionExtras(custom_agents=agents, derived_spec_snapshot=snapshot)

    def session_mcp_servers(
        self,
        requested: list[dict[str, object]],
        *,
        agent_capabilities: dict[str, object],
    ) -> list[dict[str, object]]:
        # Pass through unchanged. The Kiro BYO ACP process validates transport
        # support and fails session creation explicitly for unsupported HTTP/SSE;
        # it never silently drops a server.
        return requested

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        return ()

    async def answer_request(self, method: str) -> dict[str, object]:
        raise NotImplementedError(f"Kiro BYO answers its own host requests; {method!r} is not Crew's")

    @property
    def notification_aliases(self) -> NotificationAliases:
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    @property
    def teardown(self) -> TeardownPolicy:
        return TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)
