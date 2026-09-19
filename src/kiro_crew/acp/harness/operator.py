"""The one concrete harness that serves an OPERATOR-DEFINED (config) backend.

Every bundled harness is a hand-written class (``KiroHarness``, ``KasHarness``,
``CodexHarness``) because each answers its fifteen seams with judgement a table
cannot hold -- kiro-cli's pre-spawn gates, KAS's spec projection, codex's
transport narrowing. :class:`DescriptorHarness` is the opposite shape: it is
built from a :class:`~kiro_crew.acp.harness.descriptor.HarnessDescriptor` and
answers every seam from that data, so an ACP host an operator already runs can be
served with no per-host code branch. It is the runtime-side (path A) half of the
config-authored-backend feature; the descriptor schema (W1-B) and the
registration seam (W1-A) are its inputs.

Posture: a GENERIC, plain-ACP host, modelled on ``codex.py`` rather than on the
kiro family. A descriptor names no ``_kiro.dev`` vocabulary and registers no
agents over the wire, so the defaults here are the plain-ACP ones: standard
``session/update`` notifications, an ordinary ``session/cancel`` teardown, empty
session extras, no host-answered methods. What the descriptor DOES carry moves
onto the seam that reads it:

* ``resolve_spawn`` renders the descriptor's argv template after resolving its
  executable with a GENERIC binary resolver (env override -> mise -> augmented
  PATH -- the plain-binary ladder ``_resolve_opencode_bin`` walks, reused rather
  than re-authored), and -- for a ``session_config``-routed descriptor -- resolves
  the SAME routing-keyed credential mask ``codex.py`` resolves, refusing a tier
  that would drop it.
* ``apply_spawn_env`` always strips kiro-cli's API key: a foreign binary must
  never receive it, the same positive action ``codex.py`` / ``kas.py`` take.
* ``session_mcp_servers`` is a transform whose SHAPE the descriptor's
  ``mcp_delivery`` chooses: ``agent_file`` passes the caller's array through
  unchanged (the kiro-family posture), ``session_array`` narrows it against the
  transports the handshake advertised (the codex posture, reusing
  ``drop_unadvertised_transports``). It never ADDS -- the contract requires a
  transform, and a descriptor host with no agent spec may have to narrow.
* ``verifies_agent_activation`` is True only for an ``agent_spec``-routed
  descriptor that actually puts an agent in its argv (``agent_args`` non-empty):
  that is the only configuration where a ``--agent``-style selection was made at
  spawn for a later check to confirm, mirroring ``kiro.py`` True /
  ``codex.py``/``kas.py`` False.

The remaining seams inherit :class:`MembershipHarness`'s membership lookups. A
registered descriptor id is in none of the ``ACP_BACKENDS_*`` capability sets, so
``internal_sandbox`` / ``pod_home_remap`` / ``reads_markdown_agent_specs`` all
answer False and ``reclaim_policy`` passes the operator's thresholds through --
which is exactly the fail-safe posture a host that has demonstrated nothing
should get.

A descriptor never names code (the schema has no ``adapter`` key, by design), so
this class is the ONLY code a config-authored backend runs. It carries the
descriptor as its one piece of instance state -- the sole harness that does --
because the runtime resolves a harness by backend id and the id alone cannot
reconstruct the argv template or the routing. That state is read-only (the
descriptor is frozen) and set once at construction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kiro_crew.acp.harness._common import MembershipHarness
from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.harness.descriptor import (
    MCP_DELIVERY_SESSION_ARRAY,
    HarnessDescriptor,
    render_argv,
)
from kiro_crew.acp.types import ACP_CLIENT_CAPABILITIES, METHOD_CANCEL, METHOD_SESSION_UPDATE

__all__ = ["DescriptorHarness"]


class DescriptorHarness(MembershipHarness):
    """A generic ACP host, built from a :class:`HarnessDescriptor`.

    Constructed by the registry (W2 registration wiring) with the descriptor the
    operator authored; ``backend`` is the descriptor id so every membership
    lookup on the base class resolves through it exactly as a bundled harness's
    does.
    """

    def __init__(self, descriptor: HarnessDescriptor) -> None:
        # The one harness that holds instance state, and it holds only this: the
        # descriptor cannot be reconstructed from the backend id, and the runtime
        # resolves a harness by id. Frozen, so a session bound to it cannot have
        # its argv or routing edited underneath it.
        self._descriptor = descriptor
        self.backend = descriptor.id

    # ── Seam 1: spawn ──

    async def resolve_spawn(self, ctx: SpawnContext) -> SpawnPlan:
        """Render the descriptor's argv, plus the mask an enforced descriptor needs.

        The executable is resolved with a GENERIC plain-binary ladder (env
        override -> mise -> augmented PATH) reused from :mod:`kiro_crew.acp.client`
        rather than re-authored here, so an operator's ``executable`` resolves the
        same way opencode's and goose's binaries do. A bare name that does not
        resolve aborts the spawn with a "not found (searched ...)" message naming
        the directories walked -- the same operator-facing contract every bundled
        resolver keeps.

        The argv itself comes from :func:`render_argv`, which substitutes the
        resolved absolute path for ``{executable}`` (so the file that is
        trust-attested is the one that execs, not a PATH name re-resolved at
        exec), and emits the ``agent_args`` / ``model_args`` blocks only when an
        agent or model is actually selected.

        For a ``session_config``-routed descriptor the credential mask is resolved
        HERE, in the same thread-hop budget as the binary search, and a refusal
        aborts the spawn: such a host's privileged tools ask over the session
        config option, not by construction, so ACP cannot make it ask about a
        passive read and the OS-boundary mask is the compensating control. The
        mask is keyed on the descriptor's ROUTING, exactly as ``codex.py`` keys
        it -- :func:`kiro_crew.acp.harness.codex.resolve_spawn_masks` re-checks
        ``acp_tool_gate.is_enforced`` itself, so it returns an empty mask for an
        ``agent_spec`` descriptor and the real mask for a ``session_config`` one,
        with no identity test here.

        ``host_auth`` stays False: a descriptor declares no host-auth callback
        channel, so a process started expecting to answer one would wait for a
        frame that never arrives.
        """
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.session_handle import AcpRuntimeError

        exe, search_path = await asyncio.to_thread(
            client_mod.resolve_descriptor_executable, self._descriptor.executable
        )
        if not exe:
            raise AcpRuntimeError(
                client_mod.descriptor_executable_not_found_message(
                    self._descriptor.id, self._descriptor.executable, search_path
                )
            )
        argv = render_argv(
            self._descriptor,
            executable=exe,
            agent=ctx.agent,
            model=ctx.model or "",
            workdir=str(ctx.work_dir) if ctx.work_dir is not None else "",
        )
        # Routing-keyed mask, never keyed on this descriptor's identity: the codex
        # helper re-checks ``is_enforced`` for itself, so an ``agent_spec``
        # descriptor gets an empty mask (byte-identical argv) and a
        # ``session_config`` one gets the real mask -- and refuses a tier that
        # would drop it.
        from kiro_crew.acp.harness.codex import resolve_spawn_masks

        hidden, expose = await resolve_spawn_masks(self.backend, ctx.sandbox_mode)
        return SpawnPlan(argv=argv, extra_hidden_dirs=hidden, extra_expose_files=expose)

    def apply_spawn_env(self, env: dict[str, str]) -> None:
        """Take kiro-cli's API key OUT of the child's environment.

        A descriptor host is a foreign binary; it must never receive kiro-cli's
        model credential. Stripping it is the positive action, the same one
        ``codex.py`` and ``kas.py`` take through the same helper, so a descriptor
        process sees the same scrubbed environment.
        """
        from kiro_crew.config.loader import strip_kiro_cli_api_key

        strip_kiro_cli_api_key(env)

    @property
    def verifies_agent_activation(self) -> bool:
        """True iff an agent was actually selected at spawn for a later check to confirm.

        Two conditions, both required. The descriptor must be ``agent_spec``-routed
        (its permission decision travels as an agent selection), AND it must carry
        an ``agent_args`` block -- the argv fragment that puts the agent name on the
        command line. Without the block nothing was selected at spawn, so there is
        no activation to confirm (the ``codex.py`` / ``kas.py`` answer); with it the
        spawn is the only thing that selected the agent, so the activation must be
        verified (the ``kiro.py`` answer).

        Read through the routing TABLE rather than a stored copy, so this harness
        does not become a second declaration of routing free to disagree with the
        one the drivers read.
        """
        from kiro_crew.agent_sdk.backends import Routing, routing_for

        return routing_for(self.backend) is Routing.AGENT_SPEC and bool(self._descriptor.agent_args)

    # ── Seam 2: initialize ──

    @property
    def protocol_version(self) -> Any:
        """Plain ACP v1, as an integer -- the codex/claude spelling.

        A generic ACP host speaks the numbered revision, not kiro-cli's date
        string. Fixed rather than a descriptor field: a host that needed a
        different protocolVersion would need bespoke handshake code a descriptor
        cannot carry, so the honest default is the one plain-ACP version this core
        drives, and a mismatch fails loudly at handshake rather than silently.
        """
        return 1

    @property
    def client_capabilities(self) -> dict[str, Any]:
        """The shared plain-ACP client capabilities.

        The same object the codex harness sends: a descriptor host advertises no
        ``_kiro`` extension channel, so it gets the standard capabilities and
        nothing host-specific.
        """
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
        """Empty. A descriptor host registers no agents over the wire.

        ``custom_agents`` is the kiro-family channel for a host that takes its
        agent from ``session/new`` rather than a spawn flag; a descriptor declares
        no such channel. An ``agent_spec`` descriptor puts its agent on the
        command line (``agent_args``), so there is nothing to register per session
        -- the same empty answer codex and kiro give, for the two different reasons
        they give it.
        """
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """A TRANSFORM whose shape the descriptor's ``mcp_delivery`` chooses.

        ``agent_file`` (the default): passthrough. The host reads its MCP servers
        from its own agent-spec/config channel, so the ``session/new`` array is an
        override of same-named entries and returning the caller's list UNCHANGED is
        what keeps the request byte-identical -- the kiro-family posture.

        ``session_array``: narrowing. The host learns its servers only from this
        array, so one element whose transport it never advertised can fail the
        WHOLE ``session/new``; the array is narrowed against the capabilities THIS
        session's handshake reported, exactly as ``codex.py`` narrows it, reusing
        ``drop_unadvertised_transports``. An UNKNOWN handshake (absent or non-dict
        ``mcpCapabilities``) passes the array through untouched -- empty means
        "nothing is known", never "nothing is supported", so narrowing to nothing
        there would strip every tool from every session.

        Never an ADDITION: the descriptor cannot make this seam mount a server the
        caller did not request, which is what the contract's transform-not-addition
        rule requires.
        """
        if self._descriptor.mcp_delivery != MCP_DELIVERY_SESSION_ARRAY:
            return requested
        advertised = agent_capabilities.get("mcpCapabilities")
        if not isinstance(advertised, dict) or not advertised:
            return requested
        from kiro_crew.providers.mirrors.codex import drop_unadvertised_transports

        return drop_unadvertised_transports(list(requested), dict(advertised))

    # ── Seam 4: inbound requests the host answers ──

    @property
    def host_answered_methods(self) -> tuple[str, ...]:
        """None. A descriptor host asks Crew for nothing at the connection level.

        Empty rather than absent, so a reader can tell "this host needs no
        callback" from "nobody checked". A descriptor declares no host-auth
        callback channel; the KAS access-token callback is a kiro-family
        extension, not something a generic host sends.
        """
        return ()

    async def answer_request(self, method: str) -> dict[str, Any]:
        """Never called: :attr:`host_answered_methods` is empty.

        Raises rather than returning ``{}`` -- an empty result would let a frame
        this harness never claimed be answered as if it had.
        """
        raise NotImplementedError(
            f"descriptor harness {self.backend!r} answers no inbound request, "
            f"including {method!r}"
        )

    # ── Seam 5: notification aliases ──

    @property
    def notification_aliases(self) -> NotificationAliases:
        """Plain ACP, with no forked spellings.

        ``session/update`` only, like ``codex.py``: a descriptor host is not
        reached through kiro-cli's relay, so it sends no ``_kiro.dev`` alias,
        announces no subagent roster, and stages no MCP-init frame. Declared
        explicitly rather than inherited from the kiro family, whose vocabulary
        exists only because KAS is reached through kiro-cli.
        """
        return NotificationAliases(session_update=(METHOD_SESSION_UPDATE,))

    # ── Seam 6: teardown ──

    @property
    def teardown(self) -> TeardownPolicy:
        """An ordinary ``session/cancel`` notification; the caller then drops the session.

        A generic ACP host has no delete verb of its own -- ``_kiro.dev`` and
        ``_kiro`` teardown verbs are kiro-family extensions, and sending either
        would draw a method-not-found. ``session/cancel`` carries no id and the
        host answers nothing, so it is a NOTIFICATION: a caller that waited for a
        reply would spend its whole teardown budget on every eviction. The same
        answer codex gives, for the same reasons.
        """
        return TeardownPolicy(method=METHOD_CANCEL, notification=True)
