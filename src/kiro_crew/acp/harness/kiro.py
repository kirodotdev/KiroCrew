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
from kiro_crew.acp.harness._common import (
    KIRO_FAMILY_ALIASES,
    MembershipHarness,
    apply_mandatory_mcps_env,
)
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

        # The derived-spec freshness gate is deliberately NOT here, and this is the
        # only host-level gate that is not: it is the same check for every host, and it
        # returns a snapshot the POST-handshake check must compare against. Both ends of
        # that bracket therefore have to be owned by the object that drives the
        # handshake -- ``AcpRuntime`` -- and it runs the gate once, after this method
        # returns, as the last verification before the process is created. A second call
        # here would re-derive between the runtime's capture and the spawn, so the child
        # would load the NEWER spec while the post-handshake check compared against the
        # older snapshot and killed a perfectly valid session. The self-heal above stays
        # here because it is host-specific: kiro-cli needs the file on disk for
        # ``--agent``, and it runs BEFORE the gate for that reason -- a missing default
        # is repaired rather than refused.
        try:
            await asyncio.to_thread(agent_mod.require_fork_governance, ctx.agent, ctx.work_dir)
        except ForkGovernanceUnresolved as exc:
            raise AcpRuntimeError(str(exc)) from exc

        overlap = await asyncio.to_thread(
            sandbox_mod.delegated_workspace_exposes_sealed_target, ctx.work_dir
        )
        if overlap:
            raise AcpRuntimeError(overlap)

        argv = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", ctx.agent]
        if ctx.model:
            # Pinning at process start is the ONLY reliable way to run a
            # non-default provider model: a later set_model cannot cross provider
            # boundaries, and an agent config may pin one of its own.
            argv += ["--model", ctx.model]
        native_documents: tuple[tuple[str, str], ...] = ()
        if ctx.member_context:
            from kiro_crew.member_essential_context import kiro_launch_documents

            native_documents = tuple(
                await asyncio.to_thread(
                    kiro_launch_documents,
                    ctx.agent,
                    str(ctx.work_dir) if ctx.work_dir is not None else None,
                )
            )
        return SpawnPlan(argv=argv, native_context_documents=native_documents)

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Hand kiro-cli the API key, and exempt Crew's own MCP servers from
        Tool Search deferral.

        Deferred import: the config loader pulls in the credential path, which the
        boot path must not touch at module scope.

        **Why the exemption is here and not at a call site.** Loading a deferred
        MCP spec REWRITES the request's ``tools`` array, and an extended-thinking
        model's thinking blocks carry a signature bound to the array they were
        minted under. Replay one across a load and the provider rejects the whole
        request -- "The ``tools`` list differs from the one this block was created
        with" -- and because it is rejecting the conversation's history, every later
        turn fails identically. The session is bricked, not slowed.

        Crew's own servers are the ones that churn it: they are the infrastructure
        an agent reaches for in nearly every session, so deferring them bought very
        little and rewrote the array constantly. Third-party servers keep deferring
        -- they carry most of the spec weight and are reached rarely.

        This hook is the only place both kiro spawn paths meet. A session-serving
        child comes from ``AcpRuntime`` (kiro is in ``ACP_BACKENDS_ACP_RUNTIME``,
        and ``_start_kiro_runtime_impl`` never spawns its ``AcpClient``), while the
        auxiliary ``AcpClient`` children do not pass through here at all -- they run
        tool-less agents, so deferral has nothing to defer for them. Setting it at
        either call site would have missed the children that matter.

        The exemption's own rules -- ambient value versus per-session overlay, and
        why KAS gets it too -- live in :func:`apply_mandatory_mcps_env`, which both
        kiro-family harnesses call so the two cannot drift.
        """
        from kiro_crew.config.loader import inject_kiro_cli_api_key

        inject_kiro_cli_api_key(env)
        apply_mandatory_mcps_env(env)

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
        session_key: str = "",
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

        A request: kiro-cli answers it, and the answer is what says the session left
        the process rather than that the write reached the pipe.
        """
        return TeardownPolicy(method=METHOD_SESSION_TERMINATE, notification=False)
