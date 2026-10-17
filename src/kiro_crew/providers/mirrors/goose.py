"""Projects the agent spec onto ``goose acp``.

The second single-binary spec harness to carry a session ``mcpServers`` array, and
the array's MECHANICS are the same ones :mod:`kiro_crew.providers.mirrors.opencode`
already owns: the same shared translation
(:func:`kiro_crew.acp.session_mcp.session_mcp_servers`, no new translator), the same
``tools`` allowlist, the same registry filter, the same control-plane re-derivation,
and the same one-owner rule for the pooled broker stubs. So this module DELEGATES to
that one's module-level functions rather than restating them, and carries what is
actually goose's: the rulings prose, and the two wire facts that differ.

What differs, and neither changes the projection:

* The tool-name grammar is ``<server>__<tool>`` -- two underscores, and no ``mcp__``
  prefix -- where opencode fuses with one. goose also carries the pair separately as
  ``_meta.goose.toolCall.toolName`` and ``extensionName``.
* An element whose command cannot start is DROPPED rather than failing
  ``session/new`` whole.

The duplication that remains between the two harnesses is the mirror CLASS, not the
mechanics: a shared base for "single binary, spec dialect, session array" is a
refactor of both, which is not this change's to make.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE
from kiro_crew.providers.mirrors.base import AgentConfigMirror, Concern
from kiro_crew.providers.mirrors.base import Disposition as _D
from kiro_crew.providers.mirrors.base import Ruling, SessionProjection
from kiro_crew.providers.mirrors.opencode import place_single_binary_array

__all__ = ["GooseMirror", "goose_projection"]


def goose_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The whole goose array -- spec translation AND pooled stubs.

    Placed by the sibling harness's own function
    (:func:`~kiro_crew.providers.mirrors.opencode.place_single_binary_array`), because
    every placement rule is one this transport needs for the SAME measured reason: one
    owner for both halves of the array (a stub appended after the projection withheld
    that name would un-withhold it, and the stub is the unrestricted server).

    What differs from opencode is the restriction. Every server the spec narrows per
    tool is withheld WHOLE, Crew's own control plane included, and ``denied_tools``
    stays empty. goose's per-call channel is not safe enough to keep a narrowed server
    mounted: its ``permission.yaml`` can pre-approve a tool so it never asks, and the
    agent's own shell can write that file mid-session, after any check Crew makes.

    Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    narrowed = frozenset(server for server, _tool in projection.disabled_tools)
    out = place_single_binary_array(
        projection,
        label="goose",
        unhonoured=narrowed,
        stub_elements=stub_elements,
        session_key=session_key,
        channel_id=channel_id,
        session_token=session_token,
    )
    return SessionProjection(
        params={"mcpServers": out},
        disabled_servers=projection.disabled_servers,
        restricted_servers=narrowed,
        unhonoured_servers=narrowed,
        zero_tools=projection.zero_tools,
        derived_spec_snapshot=projection.derived_spec_snapshot,
    )


class GooseMirror(AgentConfigMirror):
    """Projects the agent spec onto ``goose acp``."""

    backend = ACP_BACKEND_GOOSE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with NO new translator and then "
                "narrowed the way the sibling single-binary harness narrows it. The "
                "channel is measured as a ROUND TRIP rather than as an accepted "
                "element, which is the distinction this folder exists to draw: driven "
                "against goose 1.50.1, the element Crew already emits is accepted, the "
                "named stdio child is asked initialize, notifications/initialized, "
                "tools/list AND tools/call by goose itself, and the tool's own result "
                "comes back on tool_call_update. Its initialize advertises "
                "mcpCapabilities of http and sse with no stdio flag, which is not a "
                "refusal: ACP's McpCapabilities schema has only those two boolean "
                "fields, so a conforming agent cannot advertise stdio and that answer "
                "is what full support looks like",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "into the allowlist that decides which servers enter the array -- the "
                "shared translation's own rule, not a second one here",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "by withholding the server it narrows, Crew's own control plane "
                "included, and declared as per_tool_deny=whole-server on the projection "
                "record. A per-call refusal is reachable -- goose asks for every MCP call "
                "under GOOSE_MODE=approve and names the pair as "
                "_meta.goose.toolCall.extensionName and toolName -- but it is not safe to "
                "keep a narrowed server mounted on it yet. goose's own permission.yaml "
                "can list a tool under always_allow so goose runs it without asking, and "
                "the agent's shell can write that file mid-session, after any check Crew "
                "makes at spawn. Withholding the server is the only form no such write "
                "can undo. Follow-up: trust only the _meta pair (refuse when absent) AND "
                "have the sandbox deny writes to goose's config directory; then the "
                "verdict can become per-call",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "as a session config option, from the provider and model selects the "
                "harness advertises on session/new -- not from this array",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the harness advertises its own provider and model vocabulary on "
                "session/new, so a Crew-side list would be a second answer to a "
                "question the session already answers",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "this harness's approval granularity is a session MODE, not a per-tool "
                "value, and the only mode Crew puts on the wire is the one that asks. "
                "An auto-approve projection would have to name auto, which "
                "session/set_mode accepts and which suppresses the permission frames "
                "the host gate is reached through",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "not projected as spec data. The mode Crew requires travels as GOOSE_MODE "
                "in the child's environment and is read back off the session response; "
                "putting it here as well would give one guarantee two owners",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "no definition is projected in any shape, so there is nothing for a "
                "prompt to ride on; the session's instructions arrive as the turn's own "
                "text",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "no channel is advertised for them, and the array carries servers rather "
                "than resources",
            ),
            Concern.HOOKS: Ruling(
                _D.TRANSLATED,
                "the ACP session/new element set has no hooks field, so the harness never "
                "receives the spec's hooks block, and Crew's turn loop runs it instead "
                "(agent_sdk/spec_hooks.py, ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS), as it does "
                "for KAS. That is sound here because every tool call arrives as a "
                "permission request, which is where a PreToolUse hook runs and can block. "
                "A tool matcher is written in kiro-cli's names; the tool name goose "
                "states in _meta.goose.toolCall is mapped back to them "
                "(acp/harness_tool_names.py), so execute_bash meets goose's shell. "
                "This covers the chat, subagent and task-runner turn loops; a "
                "channel-agent turn runs no script hooks on any backend",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        session_token: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this goose session.

        ``permission_surface_owned`` is accepted and IGNORED (it arrives in
        ``kwargs``), which is the documented behaviour for a mirror outside claude's
        class. That flag exists because claude's permission surface is a file Crew may
        not own, so a pre-approved tool there never sends
        ``session/request_permission``. goose has no such file in play: its routing is
        one environment variable, read back off the session response before the first
        prompt, so a session that reaches this point is one whose every tool call asks.

        ``stub_elements`` are the shared gateway's broker stubs for this session, which
        the caller holds and this mirror places, so the withhold rule covers both halves
        of the array -- see :func:`goose_projection`.

        Blocking -- it reads the agent spec. The caller warms this on the goose spawn
        path and serves the shared ``session/new`` call site from that cache
        (harness-parity H13).
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        ).params

    def session_projection(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        stub_elements: Collection[Mapping[str, Any]] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        session_token: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`goose_projection`, with ``kwargs`` ignored as
        :meth:`session_params` documents. ``denied_tools`` is empty on this backend by
        decision, not by default -- see the ``disabledTools`` ruling."""
        del kwargs
        return goose_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
