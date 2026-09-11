"""The KAS relay, reached through kiro-cli's own ACP transport.

KAS shares kiro-cli's binary and its ``_kiro.dev`` notification vocabulary, and
differs from it on four things that matter.

It takes no ``--agent`` flag, so the agent definition travels on every session
start and has to be re-sent on resume. Its ``protocolVersion`` is an integer, not
a date string. It can ask Crew for the access token instead of holding one. And
its teardown verb DESTROYS the session record rather than evicting it.
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
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp.harness._common import KIRO_FAMILY_ALIASES, MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.kas_agents import KasAgentTranslationError
from kiro_crew.acp.kas_host_auth import answer_get_access_token, vault_holds_identity_off_loop
from kiro_crew.acp.kas_transport import METHOD_KAS_AUTH_GET_ACCESS_TOKEN, build_kas_argv
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    KAS_CLIENT_CAPABILITIES,
    METHOD_KAS_SESSION_DELETE,
)
from kiro_crew.agent import ForkGovernanceUnresolved
from kiro_crew.config import paths as paths_mod
from kiro_crew.mcp_gateway import session_servers as session_servers_mod

logger = logging.getLogger(__name__)

__all__ = ["PROTOCOL_VERSION_KAS", "KasHarness"]

#: KAS numbers ACP revisions. It rejects the date-string spelling kiro-cli takes,
#: so the TYPE here is part of the contract, not an encoding detail.
PROTOCOL_VERSION_KAS = 1


class KasHarness(MembershipHarness):
    """The KAS relay host."""

    backend = ACP_BACKEND_KAS

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """``kiro-cli`` in relay mode, with the auth owner decided per spawn.

        No ``--agent`` and no ``--model``: KAS takes custom agents over the wire
        in ``session/new``, not from a flag.

        Crew owns the credential when its own vault holds a signed-in identity,
        and kiro-cli owns it otherwise. Deciding that per spawn is what makes a
        dashboard sign-in or sign-out take effect on the next process, and the
        answer rides on the plan so the reader loop answers the engine's callback
        only on a process that was started expecting it to.
        """
        from kiro_crew.acp.client import _resolve_kiro_bin_for_spawn, kiro_cli_not_found_message
        from kiro_crew.acp.session_handle import AcpRuntimeError

        kas_bin = await _resolve_kiro_bin_for_spawn(environ=dict(ctx.environ), home=ctx.home)
        if not kas_bin:
            raise AcpRuntimeError(
                await asyncio.to_thread(
                    kiro_cli_not_found_message, environ=dict(ctx.environ), home=ctx.home
                )
            )

        # Reads the vault off the loop and never raises.
        host_auth = await vault_holds_identity_off_loop()
        if host_auth:
            logger.info(
                "KAS auth owner=crew — Crew vault holds an identity; relay spawned "
                "without --auth-method cli (agent=%s)",
                ctx.agent or "<none>",
            )
        return SpawnPlan(argv=build_kas_argv(kas_bin, host_auth=host_auth), host_auth=host_auth)

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Take the API key OUT of the child's environment.

        The relay expects an OIDC bearer from the callback, not a Crew API key,
        and an ambient key would be sent with the wrong token type. Removing it
        is the positive action here, not an omission.
        """
        from kiro_crew.config.loader import strip_kiro_cli_api_key

        strip_kiro_cli_api_key(env)

    @property
    def verifies_agent_activation(self) -> bool:
        """No -- the activation is an explicit ``set_mode`` whose response answers it.

        There is no spawn flag whose effect could go unconfirmed.
        """
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION_KAS

    @property
    def client_capabilities(self) -> dict[str, Any]:
        return KAS_CLIENT_CAPABILITIES

    # ── Seam 3: session/new and session/load extras ──

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
    ) -> SessionExtras:
        """Project the agent spec onto KAS, for both session start paths.

        KAS registers client agents per session and has no ``--agent`` flag, so a
        session that is not handed them advertises only the modes it finds on
        disk -- and that set is NOT a superset of what it had, because KAS skips a
        profile written for kiro-cli. On resume that made the requested mode
        genuinely absent and the load refused.

        Two failures raise rather than degrade, and both mean "do not create this
        session": ungoverned fork grants would be projected over the wire, and a
        failed translation would leave the session on KAS's own default mode,
        which for a restricted agent is a BROADER agent than the caller asked for.
        """
        from kiro_crew.acp.session_handle import AcpRuntimeError

        if not agent:
            return SessionExtras()

        def _build() -> list[dict[str, Any]]:
            agent_mod.require_fork_governance(agent, work_dir)
            try:
                agent_mod.ensure_agent_materialized(agent)
            except Exception:
                logger.warning(
                    "pre-session agent materialization failed for %r", agent, exc_info=True
                )
            try:
                # A session-injected server outranks an agent-declared one, so
                # declaring both is a double registration. Only the caller holds
                # the overlay that answers which servers those are.
                stubbed = session_servers_mod.injection_server_names(mcp_gateway_overlay, agent)
            except Exception:
                # Empty is the SAFE direction: it declares a stubbed server twice
                # (the injection still wins) rather than withholding one nothing
                # else will supply.
                logger.debug(
                    "stubbed-server lookup failed for %r; projecting every declared server",
                    agent,
                    exc_info=True,
                )
                stubbed = frozenset()
            if member_dispatch:
                # The member's dashboard server arrives as a session-level entry
                # too, so it joins the subtraction set for the same reason: an
                # identity-less spec declaration could otherwise shadow the
                # member-keyed entry.
                from kiro_crew.members import MEMBER_DISPATCH_SERVER

                stubbed = frozenset(stubbed) | {MEMBER_DISPATCH_SERVER}
            return kas_agents_mod.build_kas_custom_agents(
                paths_mod.kiro_agents_dir(),
                agent,
                stub_server_names=stubbed,
                member_dispatch=member_dispatch,
            )

        try:
            return SessionExtras(custom_agents=await asyncio.to_thread(_build))
        except ForkGovernanceUnresolved as exc:
            raise AcpRuntimeError(str(exc)) from exc
        except KasAgentTranslationError as exc:
            raise AcpRuntimeError(f"cannot project agent {agent!r} onto KAS: {exc}") from exc

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
        """The access-token callback, when this backend is in the callback set.

        Resolved from the membership set rather than asserted, so the harness and
        the runtime's reader-loop guard cannot disagree about who answers what.
        """
        if self.backend not in ACP_BACKENDS_HOST_AUTH_CALLBACK:
            return ()
        return (METHOD_KAS_AUTH_GET_ACCESS_TOKEN,)

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Build the access-token response from Crew's vault.

        The result is never cached here and never logged. Raises
        ``HostAuthCallbackError`` with a token-free message, which the runtime
        turns into a JSON-RPC error -- the engine reads that as an expired
        credential and shows its sign-in prompt instead of hanging on the call.
        """
        if method != METHOD_KAS_AUTH_GET_ACCESS_TOKEN:
            raise NotImplementedError(f"KAS harness does not answer {method!r}")
        return await answer_get_access_token()

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        return KIRO_FAMILY_ALIASES

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """Delete the session record outright.

        Every local transcript-retention choice is therefore a no-op on this
        host, and a later resume degrades to "conversation gone".
        """
        return TeardownPolicy(method=METHOD_KAS_SESSION_DELETE)
