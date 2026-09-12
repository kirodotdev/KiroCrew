"""The Codex ACP adapter: one Node process, N sessions, and its own confinement.

``@agentclientprotocol/codex-acp`` is a Node stdio server. It boots a single
``codex app-server`` child and translates ACP onto that server's operations. The
``codex`` CLI does not serve ACP itself -- it reads ``acp`` as a prompt -- so the
adapter is the transport, not an optimization.

**One process already hosts many sessions, which is why this harness exists.** The
adapter keeps ``this.sessions = new Map()`` and each ACP session id is a Codex
thread id on the one shared ``codex app-server`` child. N Crew sessions on one
adapter is therefore the adapter's own design, not something Crew layers on top.
:class:`~kiro_crew.acp.client.AcpClient` starts one adapter per session and pays N
Node processes and N app-servers for N sessions; ``AcpRuntime`` pays one.

**What a harness earns, and what it does not.** ``AcpProvider`` builds its own
``AcpRuntime`` per chat, so being runnable on the runtime does not by itself put two
chats on one process: which hosts multiplex N sessions onto one runtime is
``ACP_BACKENDS_SESSION_SHARING``, and codex is not a member (its teardown verb ends
a turn without evicting the session, so a shared process would accumulate
transcripts). Until that membership is earned, one codex runtime carries one
foreground session and the process count matches the ``AcpClient`` path. Running
here at all is the prerequisite, not the saving.

**One thing is global on a shared process, and a caller has to know it.**
``providers/set`` restarts the ``codex app-server`` child and then re-resumes every
thread on it. On a per-session adapter that is one user's session. Here it is EVERY
session on the process. Nothing in this module sends ``providers/set``, and nothing
should start sending it per session.

Codex is confined by Crew's sandbox and by nothing else
------------------------------------------------------
``ACP_BACKENDS_INTERNAL_SANDBOX`` does not name codex, so :attr:`internal_sandbox`
is False and Crew's own seatbelt/bubblewrap wrapper is the only OS confinement a
codex session gets. The Codex sandbox modes the adapter can apply are in-process
policy, not an OS sandbox Crew's could nest inside.

Crew's tool gate ENFORCES codex, so the spawn carries a credential mask on its
``SpawnPlan``. For an enforced host that mask is the only thing between a
third-party binary and the operator's credential homes: ACP cannot make such a host
ask about a passive read.

Two tiers hand back an UNWRAPPED child and drop that mask on the floor -- ``off``,
and a host with no sandbox backend where unsandboxed exec is opted in. An enforced
host that spawned there would run a third-party binary with the operator's credential
homes readable and nothing compensating for it, so :func:`resolve_spawn_masks`
REFUSES those tiers instead of returning an empty mask.

Permission routing is asserted per session, not seeded to a file
----------------------------------------------------------------
codex-acp's default ``agent`` mode writes inside the workspace without asking. Its
ACP v1 ``mode`` selector is the enforceable boundary, written over
``session/set_config_option`` -- ``Routing.SESSION_CONFIG`` in
``ACP_BACKEND_ROUTING``, which both drivers read for themselves. This harness
declares no routing member: a second copy of that table would be free to disagree
with the one that decides. There is no settings file to author first either, which
is why this harness has no ordering constraint the claude one has. ``read-only``
still permits passive READS; ACP v1 has no way to require a prompt for those, and
what makes the residual gap survivable is that mask.

The session array is the whole tool surface, so it is NARROWED
--------------------------------------------------------------
codex reads no agent spec, so ``session/new``'s ``mcpServers`` is everything the
session will ever have -- and one element whose transport the adapter never
advertised fails the WHOLE request with ``-32600``, taking every other server with
it. :meth:`CodexHarness.session_mcp_servers` is where that is narrowed, against the
capabilities THIS session's handshake reported.

Empty capabilities mean "nothing is known", never "nothing is supported": on a
handshake that advertised none, the array passes through untouched. Narrowing to
nothing there would strip every tool from every session.

The recycle ceiling is an open measurement, deliberately not a guess
--------------------------------------------------------------------
Seam 9 is inherited unchanged, so the operator's configured thresholds are the
whole answer today. That is the honest state rather than the right one: the runtime
default was chosen for kiro-cli, where the growth is in one child, and a codex
process holds N sessions plus a shared ``codex app-server`` that serves every
thread. The floor is higher and the growth is shared, so the same ceiling recycles
a merely-busy process and takes every session on it down. No number is written here
because none has been measured, and a plausible-looking constant would be worse
than the mismatch it hides.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew import acp_tool_gate
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_CLIENT_CAPABILITIES,
    METHOD_CANCEL,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.providers.mirrors.codex import drop_unadvertised_transports

__all__ = ["PROTOCOL_VERSION_CODEX", "CodexHarness"]

#: codex-acp numbers ACP revisions, like the claude adapter and unlike kiro-cli's
#: date string. Kept as this harness's OWN literal even though the integer matches
#: claude's today: a divergence should be a one-line edit here rather than a silent
#: downgrade of whichever harness moved first.
PROTOCOL_VERSION_CODEX = 1


async def resolve_spawn_masks(sandbox_mode: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(hidden_dirs, expose_files)`` for the spawn, or a refusal.

    Two halves of one decision, resolved together and BEFORE the process exists:

    * the credential mask Crew's gate denies an enforced adapter at the OS boundary,
      and
    * the read-only files that come back through the backend's own carve-out (the
      other half of the Bedrock trade -- ``.aws`` stays hidden and only
      ``.aws/config`` returns).

    Raises when this session would spawn the adapter with its mask dropped: several
    ``wrap_argv`` paths return without applying ``extra_hidden_dirs``, and an
    enforced adapter started unmasked has no compensating control at all, so the
    refusal belongs here rather than after the spawn.

    The first half touches the filesystem (a cold sandbox probe shells out, and the
    mask canonicalizes the home plus every env-override root), so it runs in one
    worker thread with a bounded wait -- a stalled mount would otherwise hold the
    spawn open until the startup watchdog. The second half is pure path projection
    over the mask just resolved, which is why that mask is HANDED IN rather than
    re-derived: re-deriving it would put the filesystem read back on the loop.

    Keyed on the ROUTING, never on codex's identity: the preflight re-checks
    ``acp_tool_gate.is_enforced`` itself, so this cannot mask a harness this core
    does not enforce, and a future ``SESSION_CONFIG`` harness gets the same
    treatment by declaring the same routing.
    """
    from kiro_crew.acp import client as client_mod

    hidden = await client_mod._run_preflight_bounded(
        client_mod._sandbox_preflight, ACP_BACKEND_CODEX, sandbox_mode
    )
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_CODEX, hidden)
    return hidden, expose


class CodexHarness(MembershipHarness):
    """The codex-acp host."""

    backend = ACP_BACKEND_CODEX

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """The resolved entry script, plus the mask the adapter is confined by.

        codex-acp takes no argv of its own: any invocation enters stdio-server mode
        and blocks on stdin. So there is no ``--agent`` (the adapter reads no
        ``~/.kiro/agents/<name>.json``; the session's whole MCP surface arrives in
        the ``session/new`` array instead) and no ``--model`` (the model is chosen
        per session over ``session/set_config_option``, so pinning one at process
        start would apply it to every session on this process).

        The mask is resolved HERE, in the same thread-hop budget as the argv search,
        and a refusal aborts the spawn: this host's privileged tools do not ask by
        construction, so an empty mask is a missing control rather than a
        simplification.

        ``host_auth`` stays False. codex holds its own credential and raises nothing
        at Crew, so a process started expecting to answer a callback would be
        waiting for a frame that never arrives.

        The binary resolution is delegated to :mod:`kiro_crew.acp.client` rather than
        copied. Its order is an operator-facing contract -- explicit
        ``CODEX_ACP_BIN``, then a project-local ``node_modules`` copy, then mise,
        then PATH -- and two copies of it would drift into two different answers to
        "why did it pick that one?".
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        argv, search_path = await asyncio.to_thread(client_mod._resolve_codex_acp_bin)
        if not argv:
            # The wording comes from the client too, not just the resolution: it is
            # the operator's only instruction for fixing the install, and two
            # authorings of it drift. The search path is threaded through so a
            # "searched ..." line can never name a directory the search skipped.
            raise AcpRuntimeError(client_mod.codex_acp_not_found_message(search_path))
        hidden, expose = await resolve_spawn_masks(ctx.sandbox_mode)
        return SpawnPlan(
            argv=list(argv),
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Take kiro-cli's API key OUT of the child's environment.

        A foreign adapter must never receive it, and removing it is the positive
        action here rather than an omission -- the same thing
        ``AcpClient._resolve_spawn_env`` already does for this backend, so a codex
        process started through either transport sees the same environment.

        ``CODEX_PATH`` is deliberately left exactly as the operator set it. The
        adapter ships a compatible Codex binary as an npm dependency and reads
        ``CODEX_PATH`` only to run a DIFFERENT one, so it reaches the child through
        the ambient copy; forwarding it explicitly would imply a wiring that does
        not exist.
        """
        from kiro_crew.config import loader as loader_mod

        loader_mod.strip_kiro_cli_api_key(env)

    @property
    def verifies_agent_activation(self) -> bool:
        """No -- there is no spawn flag whose effect could go unconfirmed.

        codex takes no ``--agent``, so nothing was selected at spawn for a later
        check to confirm.
        """
        return False

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        return PROTOCOL_VERSION_CODEX

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
        """Empty. codex has no custom-agent channel to register anything on.

        ``custom_agents`` registers agent definitions on a kiro-family host that has
        no ``--agent`` flag. codex has no such flag either, and no such channel --
        what it needs per session is the ``mcpServers`` array, which
        :meth:`session_mcp_servers` narrows.
        """
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Drop the elements whose transport THIS session's adapter did not advertise.

        Narrowed at session start rather than when the array was built, for a timing
        reason: the array is assembled on the spawn path, before the adapter process
        exists, while the transports it accepts are unknown until ``initialize``
        answers. Reading them here uses what this session was told instead of what
        some adapter version once said, and stays a pure in-memory pass.

        An UNKNOWN handshake passes the array through untouched -- an absent
        ``agentCapabilities``, an absent ``mcpCapabilities``, or one that is not a
        mapping. Narrowing to nothing there would strip every tool from every
        session on a host that simply did not say.

        Only ``sse`` is fatal, and the scope matters because it is tempting to
        generalise it into sending nothing at all: a malformed stdio element, or an
        array member that is not an object, leaves ``session/new`` SUCCEEDING with
        that element dropped. ``sse`` instead fails with ``-32600`` -- not the
        ``-32602`` an unadvertised transport is easy to assume -- and takes every
        other server in the array down with it.
        """
        advertised = agent_capabilities.get("mcpCapabilities")
        if not isinstance(advertised, dict) or not advertised:
            return requested
        return drop_unadvertised_transports(list(requested), dict(advertised))

    @property
    def wants_session_file_on_load(self) -> bool:
        """No -- the adapter locates the session from its id.

        It keeps its own session records, so there is no Crew-side transcript to
        name, and sending a path it cannot read would advertise a file that is not
        there.
        """
        return False

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. codex asks Crew for nothing at the connection level.

        Empty rather than absent, so a reader can tell "this host needs no callback"
        from "nobody has checked". KAS is the contrast: it raises
        ``_kiro/auth/getAccessToken`` when Crew owns the credential.
        """
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty.

        Raises rather than returning ``{}`` -- an empty result would let a frame
        this harness never claimed be answered as if it had.
        """
        raise NotImplementedError(f"codex harness answers no inbound request, including {method!r}")

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP, with no forked spellings.

        ``session/update`` only: codex sends no ``_kiro.dev`` alias, announces no
        subagent roster, and stages no MCP-init frame. Declared explicitly rather
        than inherited from the kiro family, whose ``_kiro.dev/*`` vocabulary exists
        because KAS is reached THROUGH kiro-cli's relay -- codex is not.
        """
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """An ordinary ``session/cancel``; the caller then drops the session.

        There is no delete verb to send. kiro-cli's ``_kiro.dev/session/terminate``
        and KAS's ``_kiro/session/delete`` are both kiro-family extensions, and
        sending either would draw a method-not-found while the session stayed on the
        process.

        Ending a session here neither evicts nor deletes: the adapter keeps the Codex
        thread's own record, so a dropped session is not erased from Codex -- it is
        only unreachable from Crew.
        """
        return TeardownPolicy(method=METHOD_CANCEL)
