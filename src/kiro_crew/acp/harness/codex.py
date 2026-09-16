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
``ACP_BACKENDS_SESSION_SHARING``, and codex is NOT a member. So one codex runtime
carries one foreground session, and the process count matches the ``AcpClient``
path. Running here at all is the prerequisite, not the saving.

What is missing for sharing is on Crew's side -- the shared-subagent path persists
a provider label the continuation lookup reads as kiro-cli, so ``spawn_continue`` on
a codex subagent answers ``conversation_gone``. Until that label is one the path can
read, dedicated subagent sessions are the working behaviour.

Eviction is a separate question with a separate answer. The teardown Crew sends is
``session/close``, and it evicts: after it the sessionId stops answering, so a
session Crew drops actually leaves the process. That is what admits codex to
``ACP_BACKENDS_SESSION_EVICTION``, and through it to every path that creates and
destroys sessions on a shared process -- the high-churn background handles
(``session._bg_runtime_backends``), warm pooled reuse, the entitlement probe. One
teardown verb answers for all of them, which is the point of declaring it on the
harness rather than gating each caller.

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

The session array is the whole tool surface, and the adapter will not check it
-----------------------------------------------------------------------------
codex reads no agent spec, so ``session/new``'s ``mcpServers`` is everything the
session will ever have. The adapter does not validate it: an element whose
transport it declared unsupported is ACCEPTED, and ``session/new`` returns a normal
sessionId. Captured live -- ``mcpCapabilities`` advertising ``sse: false``, an
``sse`` element sent anyway, session created; a control element typed
``nonsense-type`` created one too.

That failure mode is silence, which is why the narrowing matters. There is no error
for anyone to see and no frame to react to -- just a session running with a server
that was never wired, so a tool the operator declared is simply absent.
:meth:`CodexHarness.session_mcp_servers` is therefore the ONLY thing standing
between the array Crew composed and an array the adapter will honour, narrowed
against the capabilities THIS session's handshake reported.

Empty capabilities mean "nothing is known", never "nothing is supported": on a
handshake that advertised none, the array passes through untouched. Narrowing to
nothing there would strip every tool from every session.

The recycle ceiling is measured, and the scope is what it measures
-----------------------------------------------------------------
Seam 9 is overridden, because the runtime's default cannot express this host's
shape. The default ceiling is 500 MB over the adapter's whole descendant subtree,
which was chosen for kiro-cli, where the growth is in one child.

A codex process is three things, and only two of them are Crew's business. The
adapter is FLAT: 103 MB at zero sessions, 85 MB at eight -- it multiplexes without
growing. ``codex app-server`` grows gently and sub-linearly, +14 MB per session on
a minimal config and +37 MB with a host MCP registry. Together those two are the
core: 224 MB idle, and 522 MB at eight sessions in the worse of the two runs.

The third thing is a per-session fleet of MCP servers that ``codex`` starts from
its OWN configuration -- roughly 44 processes per session on a real host config,
none of it shared between sessions, and it appears even when Crew passes
``mcpServers: []``. That is 2751 MB at ONE session, 21551 MB at eight.

So the subtree total crosses 500 MB on the FIRST prompt of the FIRST session, on
both runs, and the default ceiling recycles a healthy process before it has served
a single turn. Raising the ceiling instead does not work either: the per-session
term is set by the user's own codex configuration, so any subtree number Crew
picks is either useless or arbitrary.

The scope is therefore the fix, and the ceiling follows from it: measure the core
(``rss_depth=1`` -- the adapter and its direct children) against 1024 MB, which
leaves real headroom over the measured 522 MB and still catches a genuine leak in
the part of the process Crew put there. The fleet is not ignored so much as
attributed: its size is a fact about the operator's codex config, and a ceiling is
not the instrument that governs it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiro_crew import acp_tool_gate
from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_CLIENT_CAPABILITIES,
    METHOD_SESSION_CLOSE,
    METHOD_SESSION_UPDATE,
)
from kiro_crew.providers.mirrors.codex import drop_unadvertised_transports
from kiro_crew.sandbox import detect_backend

__all__ = ["PROTOCOL_VERSION_CODEX", "CodexHarness"]

logger = logging.getLogger(__name__)

#: codex-acp numbers ACP revisions, like the claude adapter and unlike kiro-cli's
#: date string. Kept as this harness's OWN literal even though the integer matches
#: claude's today: a divergence should be a one-line edit here rather than a silent
#: downgrade of whichever harness moved first.
PROTOCOL_VERSION_CODEX = 1


def _sandbox_wrapper_generations(sandbox_mode: str) -> int:
    """Crew-owned processes between the spawned pid and the harness's adapter.

    ``1`` for a backend that ``fork()``s and stays resident as the parent, ``0``
    for one that execs into the target or does not wrap at all. Only a bounded RSS
    scope reads this: an unbounded measurement sums the whole subtree, where an
    extra generation at the top changes nothing.

    Failure answers ``0``, which is the fail-safe direction here. Too small an
    offset stops the sum one generation short and can only UNDER-count, so the
    ceiling is reached later rather than a healthy process being recycled early.
    """
    try:
        return 1 if detect_backend(sandbox_mode) == "namespace" else 0
    except Exception:
        logger.debug("sandbox wrapper-generation probe failed", exc_info=True)
        return 0


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

    #: Core-only RSS ceiling, in MB, measured over ``rss_depth`` below.
    #:
    #: 1024 against a measured core of 224 MB idle and 522 MB at eight sessions.
    #: Not comparable to the runtime's own 500: that one counts the whole subtree,
    #: this one counts two processes, and the module docstring has the numbers.
    CORE_RSS_CEILING_MB = 1024.0

    #: Generations below the adapter that the ceiling above is measured over. 1 is
    #: the adapter plus ``codex app-server``; the per-session MCP fleet ``codex``
    #: starts from its own config sits one generation further down and is excluded.
    CORE_RSS_DEPTH = 1

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
        # circular import: kiro_crew.acp.client imports the harness registry this
        # module is part of, so both of these resolve at call time rather than at
        # module scope.
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
        wrapper_generations = await asyncio.to_thread(
            _sandbox_wrapper_generations, ctx.sandbox_mode
        )
        return SpawnPlan(
            argv=list(argv),
            rss_depth=self.CORE_RSS_DEPTH + wrapper_generations,
            extra_hidden_dirs=hidden,
            extra_expose_files=expose,
        )

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Take kiro-cli's API key OUT of the child's environment.

        A foreign adapter must never receive it, and removing it is the positive
        action here rather than an omission.

        Three variables this host needs are left exactly as the operator set them,
        and reaching the child through the ambient copy of the environment is how
        they get there. Naming them here rather than forwarding them is the point:
        each is READ by something outside Crew, so a Crew-side default would be a
        value Crew invented for a consumer it does not own.

        ``CODEX_PATH`` names the ``codex`` binary the adapter spawns
        ``app-server`` from. Unset, the adapter runs the Codex it ships as an npm
        dependency, and on a build whose provider is reached through a wrapper that
        bundled binary fails ``session/new`` with ``-32000 Authentication
        required`` -- an auth-shaped error for what is really a wrong-binary
        condition. Set, no ``authenticate`` call is needed at all.

        ``CODEX_HOME`` moves the whole ``codex`` configuration directory. It is
        also a MEMORY lever, not only an isolation one: the per-session MCP fleet
        is started from the config found there, which is the difference between the
        two measurement runs in this module's docstring.

        ``AWS_CONFIG_FILE`` (with ``AWS_SHARED_CREDENTIALS_FILE``) is the one whose
        DIRECTORY matters as much as its value. A wrapper that writes its own
        config does so atomically -- a temporary file in that same directory, then
        a rename -- so a read-only directory fails the write, and the process dies
        before ACP is reached. Nothing here fences that directory, and nothing here
        should start: ``resolve_spawn_masks`` hides credential homes and re-exposes
        only ``.aws/config``, as a read-only COPY, so the default ``~/.aws`` is not
        a directory this child can write in.

        THE REMEDY IS THE OPERATOR'S, and it is one step: point ``AWS_CONFIG_FILE``
        and ``AWS_SHARED_CREDENTIALS_FILE`` at a writable directory OUTSIDE the
        hidden tree -- any path the mask does not cover -- which is what the probe
        that found this did. Making ``~/.aws`` itself writable is the alternative
        and is refused: it means moving that path off the read-only copy primitive
        onto one that un-hides the real credential tree, which weakens the mask for
        every session to save one step in a wrapper's setup.
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
        session_key: str = "",
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

        What an unadvertised element costs depends on its shape, and both answers
        are measured (``test_real_codex_acp_accepts_the_crew_stdio_element``). An
        element that parses as its transport -- an sse element carrying the
        schema-required ``headers`` array, which is the shape Crew's translation
        emits -- is named and refused with ``-32600``, and the refusal fails the
        WHOLE ``session/new``: every server in the array lost over one entry. An
        element that does not parse as its transport (the same sse without
        ``headers``) falls to the untagged variant and is accepted with no error at
        all, the server silently never wired. So this narrowing is load-bearing
        from both directions: on Crew's own array it is what keeps one unsupported
        entry from costing the session, and on a malformed one it is the only check
        at all, since nothing downstream can detect a server that was never wired.
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
        """The standard ACP ``session/close``, awaited, and it evicts.

        Neither kiro-family verb applies here: kiro-cli's
        ``_kiro.dev/session/terminate`` and KAS's ``_kiro/session/delete`` are
        extensions, and sending either would draw a method-not-found while the
        session stayed on the process. codex-acp implements the standard verb
        instead, and what it does with it is the fact this policy records.

        **What ``session/close`` does, measured.** The adapter drops the session from
        its local map and unsubscribes the Codex thread. Captured live against
        codex-acp 1.11.0, with ``session/set_config_option`` on the same sessionId as
        the liveness oracle:

        * before close: answers with the refreshed ``configOptions``;
        * after close (request): ``{"result": {}}`` back, then the same oracle answers
          ``-32603`` -- the sessionId is gone;
        * close again on the same id: ``{}`` again, so a double-terminate is safe;
        * a fresh ``session/new`` afterwards succeeds, so the process is unharmed.

        ``session/cancel``, on the same wire and the same oracle, leaves the session
        answering: it interrupts a turn, it does not end a session. A harness that
        sends it as its teardown does not belong in ``ACP_BACKENDS_SESSION_EVICTION``
        -- every path that creates and destroys a session on the shared process (the
        entitlement probe, warm pooled reuse, a refused routing write) leaves one
        resident under it. Sending the verb that
        evicts is what closes all of those at once.

        Evict, not delete: the Codex thread's own record survives, the same shape as
        kiro-cli's ``terminate``. ``session/delete`` would archive the thread on the
        Codex side and is not what a caller dropping a session means.

        **A REQUEST, and that is not cosmetic.** Measured on the same run: the
        adapter answers ``session/close`` with ``{}``, and sent as a NOTIFICATION it
        ignores it -- the session stays addressable exactly as it did under
        ``cancel``. So ``notification=False`` is load-bearing in the direction that
        looks like nothing is wrong: the notification form would log no error and
        leak every session. The runtime awaits this bounded by ``_TERMINATE_TIMEOUT``
        like the kiro family's verbs, and the adapter answers promptly, so the
        refusal path pays one round-trip rather than a budget.

        The gated test ``test_codex_session_mcp.py::test_real_codex_acp_session_close_evicts``
        repeats the measurement above on every install that has the adapter, so an
        adapter release that changes what ``close`` does goes red there rather than
        silently re-opening the leak.
        """
        return TeardownPolicy(method=METHOD_SESSION_CLOSE, notification=False)

    # ── Seam 9: recycle ──

    def reclaim_policy(self, *, max_age_secs: float, max_rss_mb: float) -> ReclaimPolicy:
        """Core-only RSS, against this host's own ceiling. Age passes through.

        The measurement and the numbers are in this module's docstring. In short:
        the adapter's whole subtree is dominated by a per-session MCP fleet
        ``codex`` starts from the operator's own config, so a subtree ceiling
        recycles a healthy process on its first prompt, and no subtree number Crew
        could pick fixes that -- the per-session term is not Crew's to know.

        The operator's ``max_rss_mb`` is not carried over, and that is the one place
        this seam's usual rule does not hold. Narrowing a ceiling keeps its unit;
        changing ``rss_depth`` changes it, so the incoming number describes a
        quantity this policy does not measure. Passing it through would apply a
        subtree budget to two processes.

        ``max_age_secs`` IS passed through, because nothing about age changes with
        the scope.
        """
        if max_rss_mb != self.CORE_RSS_CEILING_MB:
            # The substitution is the one place an operator's configured number does
            # not reach a host, so it is stated at INFO rather than left to be read
            # off this docstring. An operator who raised the runtime ceiling and saw
            # no change on codex has the reason in their own log, with both numbers
            # and the scope that makes them different units.
            logger.info(
                "codex reclaim: RSS ceiling %.0f MB over the core scope (depth %d), "
                "not the configured %.0f MB, which counts the whole subtree "
                "(different unit; see CodexHarness.reclaim_policy)",
                self.CORE_RSS_CEILING_MB,
                self.CORE_RSS_DEPTH,
                max_rss_mb,
            )
        return ReclaimPolicy(
            max_age_secs=max_age_secs,
            max_rss_mb=self.CORE_RSS_CEILING_MB,
        )
