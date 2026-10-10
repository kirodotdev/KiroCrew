"""The KAS relay, reached through kiro-cli's own ACP transport.

KAS shares kiro-cli's binary, but reports MCP readiness through session-scoped
``_kiro/mcp/status`` and ``_kiro/tools/didChange`` snapshots.

It takes no ``--agent`` flag, so the agent definition travels on every session
start and has to be re-sent on resume. Its ``protocolVersion`` is an integer, not
a date string. It can ask Crew for the access token instead of holding one. And
its teardown verb DESTROYS the session record rather than evicting it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

# The pre-spawn helpers are reached through their defining MODULE, not bound as
# local names: a local binding cannot be patched at its definition site, so a
# test aiming there would silently get the real filesystem work instead of a
# stub. Only the exception type is bound directly -- an exception class is
# compared by identity, never substituted.
from kiro_crew import agent as agent_mod
from kiro_crew.acp import kas_agents as kas_agents_mod
from kiro_crew.acp._dispatch import advertised_mode_origin
from kiro_crew.acp.child_env_defaults import apply_child_env_defaults
from kiro_crew.acp.harness._common import (
    KIRO_FAMILY_ALIASES,
    MembershipHarness,
    pin_mandatory_mcps_env,
)
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.kas_agents import KasAgentTranslationError, KasReservedAgentIdError
from kiro_crew.acp.kas_host_auth import answer_get_access_token, vault_holds_identity_off_loop
from kiro_crew.acp.kas_transport import METHOD_KAS_AUTH_GET_ACCESS_TOKEN, build_kas_argv
from kiro_crew.acp.session_mcp import project_agent_spec
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKENDS_HOST_AUTH_CALLBACK,
    KAS_CLIENT_CAPABILITIES,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_SESSION_DELETE,
    METHOD_KAS_TOOLS_CHANGED,
)
from kiro_crew.agent import ForkGovernanceUnresolved
from kiro_crew.config import paths as paths_mod
from kiro_crew.kiro_cli import chat_sibling
from kiro_crew.mcp_gateway import session_servers as session_servers_mod

logger = logging.getLogger(__name__)

__all__ = ["PROTOCOL_VERSION_KAS", "KasHarness", "resolve_projected_spec"]

#: KAS numbers ACP revisions. It rejects the date-string spelling kiro-cli takes,
#: so the TYPE here is part of the contract, not an encoding detail.
PROTOCOL_VERSION_KAS = 1


def _is_operator_override(binary: str, environ: Mapping[str, str]) -> bool:
    """Is *binary* the operator's own ``KIROCREW_KIRO_BIN`` choice?

    Compared as absolute paths, the way the resolver hands the override
    through (``snapshot_trusted_acp_executable`` returns ``abspath`` of the
    candidate). No ``realpath``: the operator named THIS path.
    """
    override = (environ.get("KIROCREW_KIRO_BIN") or "").strip()
    return bool(override) and os.path.abspath(binary) == os.path.abspath(override)


#: The keys a checkout's own agent spec decides for a KAS session. An allow list:
#: each one shapes what the agent says or which tools it can SEE, and none of them
#: grants anything. A spec inside a cloned repository is untrusted input
#: (``acp.session_mcp._project_mcp_trusted``), and every key left out either
#: launches a command, approves a call or reads a file on the session's behalf:
#: ``mcpServers`` (a server is a command KAS launches at ``session/new``),
#: ``allowedTools`` and ``permissions`` (auto-approval), ``hooks`` (commands Crew
#: runs), ``includeMcpJson`` and ``includePowers`` (load the checkout's own server
#: configuration), ``resources`` (files read into context) and every key added
#: later. A tool the honoured ``tools`` list reveals still resolves to ``ask``.
PROJECT_SPEC_HONOURED_KEYS = ("name", "description", "prompt", "tools", "excludedTools", "model")


def resolve_projected_spec(
    agents_dir: Path, agent: str, work_dir: str | Path | None
) -> tuple[dict[str, Any], Path]:
    """The spec KAS is handed for *agent*, and the directory its prompt anchors to.

    A name the user level declares resolves to the user-level spec in
    *agents_dir*, whatever the checkout holds. Only a name the user level does
    NOT declare is looked up in ``<work_dir>/.kiro/agents/``, through the
    resolver the mirrored hosts use (``acp.session_mcp.project_agent_spec``).

    A checkout is untrusted input, so a project-only agent's spec is its
    :data:`PROJECT_SPEC_HONOURED_KEYS` and nothing else, with no servers. Its
    ``file://`` prompt anchors to the checkout's agents directory and may not
    leave it: a rooted path is refused, because the resolved text is shipped
    over the wire and a cloned repository must not choose which of the user's
    files that is.

    Either way the servers and hooks a session runs are user-level, which is why
    the broker-overlay lookup stays unscoped for this host
    (``agent_sdk.backends.overlay_project_scope``) and why Crew's spec hooks keep
    reading the user level (``agent_sdk.spec_hooks``).

    A project-only spec that exists but cannot be read raises
    :class:`KasAgentTranslationError`: there is no other spec of that name to run.
    """
    if not kas_agents_mod.agent_spec_absent(agents_dir, agent):
        return kas_agents_mod.load_agent_spec(agents_dir, agent), agents_dir
    declared, project = project_agent_spec(agent, work_dir)
    if not declared:
        return kas_agents_mod.load_agent_spec(agents_dir, agent), agents_dir
    if project is None:
        raise KasAgentTranslationError(
            f"agent {agent!r} is declared by {work_dir}/.kiro/agents but that spec is unreadable"
        )
    prompt = project.get("prompt")
    if isinstance(prompt, str) and prompt.startswith(kas_agents_mod._PROMPT_FILE_SCHEME):
        ref = prompt[len(kas_agents_mod._PROMPT_FILE_SCHEME) :]
        # ``anchor``, not ``is_absolute``: on Windows ``/etc/x`` is rooted without a
        # drive, so it is not absolute there yet still names a file outside the checkout.
        if Path(ref).expanduser().anchor:
            raise KasAgentTranslationError(
                f"agent {agent!r} is declared by a project checkout, so its prompt must be "
                f"a path inside that checkout's .kiro/agents, not {ref!r}"
            )
    dropped = sorted(k for k in project if k not in PROJECT_SPEC_HONOURED_KEYS)
    if dropped:
        logger.info(
            "agent %r: the project checkout's spec is not trusted to grant; ignored keys: %s",
            agent,
            ", ".join(dropped),
        )
    spec = {k: project[k] for k in PROJECT_SPEC_HONOURED_KEYS if k in project}
    if work_dir is None:  # unreachable: ``project_agent_spec`` declares nothing without one
        return spec, agents_dir
    return spec, paths_mod.project_agents_dir(work_dir)


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
            # Enter through the chat binary, not the launcher. The `kiro-cli`
            # launcher checks its OWN sign-in before it execs `kiro-cli-chat`
            # for `acp`, so a Crew-owned spawn -- which exists precisely for the
            # host where kiro-cli is signed out -- dies at the launcher with
            # "You are not logged in" and never asks Crew for the credential.
            # `kiro-cli-chat acp --agent-engine v3` skips that gate and starts
            # KAS in `--auth=acp-callback`. Off the loop: a stat on the install
            # directory. The cli-owned spawn keeps the launcher untouched: it
            # needs kiro-cli signed in anyway, so the gate costs it nothing.
            # An operator's KIROCREW_KIRO_BIN is exactly what they asked for --
            # a wrapper of theirs named `kiro-cli` is never swapped out from
            # under them.
            if not _is_operator_override(kas_bin, ctx.environ):
                chat_bin = await asyncio.to_thread(chat_sibling, kas_bin)
                if chat_bin:
                    kas_bin = chat_bin
            logger.info(
                "KAS auth owner=crew — Crew vault holds an identity; relay spawned "
                "without --auth-method cli via %s (agent=%s)",
                os.path.basename(kas_bin),
                ctx.agent or "<none>",
            )
        return SpawnPlan(argv=build_kas_argv(kas_bin, host_auth=host_auth), host_auth=host_auth)

    def apply_spawn_env(
        self,
        env: dict[str, str],
        *,
        spawned_binary: str | None = None,
        cli_owned_auth: bool = False,
    ) -> None:
        """Settle kiro-cli's own ``KIRO_API_KEY`` by who owns the credential.

        Crew-owned (``cli_owned_auth`` False, the relay answers
        ``_kiro/auth/getAccessToken`` from Crew's vault): the key is taken OUT.
        The engine prefers an API key in its environment over the callback, so a
        key left set would override the identity the operator signed in with and
        keep answering after a dashboard sign-out.

        cli-owned (``--auth-method cli``): the relay is kiro-cli authenticating
        itself, and an API key IS a kiro-cli sign-in -- kiro-cli keeps no stored
        record of it, the variable is the whole login. The launcher refuses to
        start without some sign-in, and the v3 engine sends the key as its own
        ``api_key`` token type. So the key is handed over exactly as the kiro
        harness hands it: inherited, or re-read from the data home's ``.env``.

        The relay is kiro-cli, so it reads the same Tool Search never-defer list;
        :func:`pin_mandatory_mcps_env` pins it by operator override or engine version.

        ``agent.child_env_defaults`` applies here too, for the same reason: the
        relay is a kiro-cli process with the same core-count-sized pools.
        """
        from kiro_crew.config.loader import inject_kiro_cli_api_key, strip_kiro_cli_api_key

        if cli_owned_auth:
            inject_kiro_cli_api_key(env)
        else:
            strip_kiro_cli_api_key(env)
        pin_mandatory_mcps_env(env, spawned_binary=spawned_binary)
        apply_child_env_defaults(env)

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

    def record_session_projection(self, handle: Any, custom_agents: Any, active_agent: str) -> None:
        """Record what the batch this session registered auto-approves.

        KAS keeps that batch for the session's life (``set_mode`` activates an entry
        of it, it re-sends nothing), so the turn loop compares this with the
        PreToolUse hooks as they stand now, and a mode switch re-reads the entry for
        the agent it moves to.
        """
        handle.kas_auto_approved = kas_agents_mod.projected_auto_approved(
            custom_agents, active_agent
        )
        handle.kas_projected_agent = active_agent if custom_agents else ""
        handle.kas_registered_agents = list(custom_agents) if custom_agents else []

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
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

        def _build() -> tuple[list[dict[str, Any]], Any]:
            agent_mod.require_fork_governance(agent, work_dir)
            try:
                agent_mod.ensure_agent_materialized(agent)
            except Exception:
                logger.warning(
                    "pre-session agent materialization failed for %r", agent, exc_info=True
                )
            # OUTSIDE that handler, deliberately. The materialization above is
            # best-effort -- a missing default spec costs a set_mode fallback -- but a
            # STALE derived spec is the hazard itself, so its refusal must abort the
            # session the way ``require_fork_governance`` above does. Inside the block
            # the broad ``except`` would log the refusal and project the stale spec.
            try:
                snapshot = agent_mod.require_fresh_derived_spec(agent, work_dir)
            except agent_mod.DerivedSpecStale as exc:
                raise AcpRuntimeError(str(exc)) from exc
            # The spec the projection runs on comes from the GATE for a derived agent, so
            # this path reads the file ZERO times. A read placed here instead would be a
            # second observation of a file the gate had already finished with, however
            # tight the sequence looks, and a revocation landing between the two would
            # reach the session as its whole tool surface as though it had been checked.
            # No lock closes that -- both halves are this process's own reads -- so the
            # second read is removed rather than re-verified. Every other agent mirrors
            # nothing, has no snapshot, and is resolved nearest-first here.
            agents_dir = paths_mod.kiro_agents_dir()
            if snapshot is not None and snapshot.spec is not None:
                spec, prompt_dir = snapshot.spec, agents_dir
            else:
                spec, prompt_dir = resolve_projected_spec(agents_dir, agent, work_dir)
            try:
                # A session-injected server outranks an agent-declared one, so
                # declaring both is a double registration. Only the caller holds
                # the overlay that answers which servers those are.
                stubbed = session_servers_mod.injection_server_names(
                    mcp_gateway_overlay,
                    agent,
                    # Deliberately UNSCOPED. A checkout spec of this name never
                    # sets the servers: those stay the user-level spec's
                    # (``resolve_projected_spec``). Handing the
                    # checkout to a name-keyed lookup over that same user-level
                    # directory would collapse this set to empty, project the
                    # user-level servers un-subtracted, and run them outside the
                    # broker: no pool, no caller-identity attribution, no
                    # governance. ``agent_sdk.backends.overlay_project_scope``
                    # answers ``{}`` for this host on the injection half for
                    # the same reason.
                    work_dir=None,
                )
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
            if crew_panel:
                # And the panel server, for the same reason on the same path.
                from kiro_crew.members import MEMBER_PANEL_SERVER

                stubbed = frozenset(stubbed) | {MEMBER_PANEL_SERVER}
            # The snapshot travels WITH the payload it built. This payload is where the
            # spec is CONSUMED on this host -- ``set_mode`` activates what is already
            # registered and reads nothing -- so the check that proves the consumed spec
            # did not change has to compare against this snapshot rather than against a
            # fresh read of the file.
            return (
                kas_agents_mod.build_kas_custom_agents(
                    prompt_dir,
                    agent,
                    spec,
                    stub_server_names=stubbed,
                    member_dispatch=member_dispatch,
                    crew_panel=crew_panel,
                    session_key=session_key,
                ),
                snapshot,
            )

        try:
            built, built_from = await asyncio.to_thread(_build)
            return SessionExtras(custom_agents=built, derived_spec_snapshot=built_from)
        except ForkGovernanceUnresolved as exc:
            raise AcpRuntimeError(str(exc)) from exc
        except KasReservedAgentIdError as exc:
            # Already the whole instruction, in the dashboard's labels; the
            # prefix below would put wire vocabulary in front of it.
            raise AcpRuntimeError(str(exc)) from exc
        except KasAgentTranslationError as exc:
            raise AcpRuntimeError(f"cannot project agent {agent!r} onto KAS: {exc}") from exc

    def activation_refusal(self, agent: str, resp: dict[str, Any]) -> str | None:
        """Refuse an id the engine advertises as ITS OWN built-in rather than ours.

        The definition Crew sent rode ``_meta.kiro.customAgents``, so the
        advertised mode of that id should carry ``origin: client``. The stamp
        ``bundled`` -- the one value MEASURED on kiro-cli 2.23.0 for
        ``vibe``/``spec``/``plan``/... -- means the engine kept a built-in
        agent under the id and discarded the client entry: the one shape the
        is-it-advertised check cannot see, because the id IS advertised, and a
        ``set_mode`` would succeed and run the built-in under the crewmate's
        name. ``KAS_RESERVED_AGENT_IDS`` refuses the ids measured to do this
        before the wire; this reads the wire itself, so a built-in a later
        engine adds under a NEW id fails loudly instead of silently
        substituting. Only that measured stamp refuses. An absent stamp is not
        evidence either way, and a stamp this code has never seen (an engine
        that renames ``client`` to, say, ``custom``) is logged and let through:
        kiro-cli is the operator's own install, not pinned by Crew, so an
        unmeasured value must not turn into a session-start denial of every
        crewmate with a rename remedy that cannot help.
        """
        origin = advertised_mode_origin(resp, agent)
        if not origin or origin == "client":
            return None
        if origin != "bundled":
            logger.warning(
                "agent %r: the engine stamps this advertised id with an unmeasured "
                "origin=%r (neither 'client' nor 'bundled'); activating it unrefused",
                agent,
                origin,
            )
            return None
        # The refusal blames the NAME; the stamp is what justified that. Logged
        # raw so a later engine that changes its stamping (say, a disk-advertised
        # replacement stamped ``bundled``) shows up here as the same value on
        # every crewmate, instead of as a rename remedy that cannot help.
        logger.warning(
            "agent %r: the engine advertises this id as its own built-in "
            "(origin=%r); refusing to activate it",
            agent,
            origin,
        )
        return (
            f"Rename this crewmate's template: “{agent}” is reserved for a built-in "
            "agent, so the crewmate's own prompt and tools would not run under it. "
            "Both fixes are on the crewmate's Agent Template tab: for the crewmate's own "
            "copy, 'Save as new template…' under another name (it keeps its "
            "customizations) or 'Reset my changes'; for a shared template, the template "
            "picker at the top of the tab."
        )

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
        return replace(
            KIRO_FAMILY_ALIASES,
            mcp_init=KIRO_FAMILY_ALIASES.mcp_init
            + (METHOD_KAS_MCP_STATUS, METHOD_KAS_TOOLS_CHANGED),
            mcp_readiness=True,
        )

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """Delete the session record outright.

        Every local transcript-retention choice is therefore a no-op on this
        host, and a later resume degrades to "conversation gone".

        A request, like the kiro verb it stands in for: the deletion is what the
        answer confirms, and a delete whose outcome is unknown is worth waiting for.
        """
        return TeardownPolicy(method=METHOD_KAS_SESSION_DELETE, notification=False)
