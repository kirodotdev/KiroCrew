"""kiro-cli, spoken to directly.

kiro-cli takes its agent from a ``--agent`` flag and reads the spec off disk
itself, so almost everything this harness does happens BEFORE the process starts:
put the spec where kiro-cli will look, refuse the spawn when the agent's grants
are not governed, and hand over the credential the CLI expects in its own
environment variable.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

# The pre-spawn helpers are reached through their defining MODULE, not bound as
# local names: a local binding cannot be patched at its definition site, so a
# test aiming there would silently get the real filesystem work instead of a
# stub. Only the exception type is bound directly -- an exception class is
# compared by identity, never substituted.
from kiro_crew import agent as agent_mod
from kiro_crew import sandbox as sandbox_mod
from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES, MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    ACP_CLIENT_CAPABILITIES,
    METHOD_SESSION_TERMINATE,
)
from kiro_crew.agent import ForkGovernanceUnresolved

logger = logging.getLogger(__name__)

__all__ = ["KIRO_CLI_SUBCMD", "PROTOCOL_VERSION", "KiroHarness"]

#: The ACP protocol revision kiro-cli speaks, as a date string.
PROTOCOL_VERSION = "2025-08-22"

#: The subcommand that puts kiro-cli into ACP mode.
KIRO_CLI_SUBCMD = "acp"


class KiroHarness(MembershipHarness):
    """The kiro-cli host."""

    backend = ACP_BACKEND_KIRO

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """``kiro-cli acp --agent <agent> [--model <model>]``, after three gates.

        Two of the three gates REFUSE the spawn. Fork governance is not
        best-effort: a fork's on-disk ``allowedTools`` / ``autoApprove`` bypass
        the approval gate, so a pending or failed refresh would run grants nobody
        checked. And a work dir overlapping the agents tree is the one way left
        to rewrite a spec once the spawn is delegated to kiro-cli's own sandbox,
        where the launcher's seal never applies.

        Materialization is the one that is best-effort: kiro-cli discovers its
        selectable modes from ``~/.kiro/agents/*.json`` at startup, so the managed
        default spec has to exist before this spawn or a later ``set_mode`` faults
        with "Mode not found". A non-managed agent cannot be regenerated here, and
        the session-start guard fails that case closed instead.
        """
        # Imported at call time: kiro_crew.acp.client owns the trusted-binary
        # search, and importing it at module scope would make this package part
        # of that module's import cycle.
        from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn, kiro_cli_not_found_message
        from kiro_crew.acp.session_handle import AcpRuntimeError

        kiro_bin = await _resolve_kiro_bin_for_spawn(environ=dict(ctx.environ), home=ctx.home)
        if not kiro_bin:
            raise AcpRuntimeError(
                await asyncio.to_thread(
                    kiro_cli_not_found_message, environ=dict(ctx.environ), home=ctx.home
                )
            )

        try:
            await asyncio.to_thread(agent_mod.ensure_agent_materialized, ctx.agent)
        except Exception:
            logger.warning("pre-spawn agent materialization failed", exc_info=True)

        try:
            await asyncio.to_thread(agent_mod.require_fork_governance, ctx.agent, ctx.work_dir)
        except ForkGovernanceUnresolved as exc:
            raise AcpRuntimeError(str(exc)) from exc

        overlap = await asyncio.to_thread(
            sandbox_mod.delegated_workspace_exposes_agents_dir, ctx.work_dir
        )
        if overlap:
            raise AcpRuntimeError(overlap)

        argv = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", ctx.agent]
        if ctx.model:
            # Pinning at process start is the ONLY reliable way to run a
            # non-default provider model: a later set_model cannot cross provider
            # boundaries, and an agent config may pin one of its own.
            argv += ["--model", ctx.model]
        return SpawnPlan(argv=argv)

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Hand kiro-cli the API key from Crew's own configuration.

        Deferred import: the config loader pulls in the credential path, which the
        boot path must not touch at module scope.
        """
        from kiro_crew.config.loader import inject_kiro_cli_api_key

        inject_kiro_cli_api_key(env)

    @property
    def verifies_agent_activation(self) -> bool:
        """Yes -- the ``--agent`` spawn is the only thing that selected the agent.

        Nothing else confirms it took, and silently running kiro-cli's default
        mode instead of a restricted agent is a privilege escalation.
        """
        return True

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION

    @property
    def client_capabilities(self) -> dict[str, Any]:
        return ACP_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load extras ──

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
    ) -> SessionExtras:
        """Nothing. kiro-cli already has the agent from its spawn flag.

        Sending a wire-registered copy as well would advertise the same agent
        twice, so the empty answer is the correct one rather than a gap.
        """
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """The caller's list, unchanged.

        This host reads its own agent spec, so the session-level array is an
        override of same-named entries rather than the whole tool surface, and it
        accepts every transport Crew injects. Returning the same object is what
        makes the request byte-identical to one built without this seam.
        """
        return requested

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. kiro-cli holds its own credential and asks Crew for nothing."""
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        raise NotImplementedError(f"kiro-cli answers its own requests; {method!r} is not Crew's")

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        return KIRO_FAMILY_ALIASES

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """Evict the session from the shared process, leaving the transcript.

        The record stays on disk, so a later resume finds it and a caller asking
        to keep the transcript is honoured.
        """
        return TeardownPolicy(method=METHOD_SESSION_TERMINATE)
