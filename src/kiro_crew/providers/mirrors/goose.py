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

from kiro_crew.agent_sdk.backends import ACP_BACKEND_GOOSE
from kiro_crew.providers.mirrors.base import AgentConfigMirror, Concern
from kiro_crew.providers.mirrors.base import Disposition as _D
from kiro_crew.providers.mirrors.base import Ruling, SessionProjection
from kiro_crew.providers.mirrors.opencode import opencode_projection

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

    The sibling harness's projection verbatim, because every rule it applies is a rule
    this transport needs for the SAME measured reason rather than by resemblance: one
    owner for both halves of the array (a stub appended after the projection withheld
    that name would un-withhold it, and the stub is the unrestricted server), a
    third-party server narrowed per tool withheld whole, and Crew's own control plane
    withheld on the same terms when an operator narrowed it deliberately.

    Named here rather than imported at the call site so a reader looking for goose's
    projection finds goose's name, and so the day the two stop agreeing this is the one
    function that changes.
    """
    return opencode_projection(
        agent,
        stub_server_names=stub_server_names,
        stub_elements=stub_elements,
        work_dir=work_dir,
        session_key=session_key,
        channel_id=channel_id,
        session_token=session_token,
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
                "record. This is a CONSERVATIVE choice, not a forced one. The channel is "
                "on the wire -- _meta.goose.toolCall.toolName and extensionName on the "
                "tool_call frame -- and Crew reads it: the identity table in acp._dispatch "
                "carries a row for it, so a denied (server, tool) pair does match on the "
                "per-call path. What is missing is the other half of what codex has: a "
                "PROJECTION that mounts a narrowed server while filtering its denied "
                "tools out of the array. Until that exists, withholding the whole server "
                "is the choice that cannot leave a switched-off tool reachable, and it is "
                "the conservative direction of the two. Follow-up: give this harness a "
                "per-tool projection and the verdict becomes TRANSLATED per tool",
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
                _D.NO_CHANNEL,
                "the one open gap, and it is the same one every spec adapter records: the "
                "ACP session/new element set has no hooks field, so a user's per-agent "
                "hooks block reaches kiro-cli and no other backend. Crew's OWN hooks "
                "(hooks.py, fired on ACP tool events) are unaffected and already work on "
                "this backend, because every tool call arrives as a permission request; "
                "this gap is only the spec block",
                channel="a hooks field on the goose session/new element set, or this "
                "harness's own config.yaml -- which Crew deliberately does not write, "
                "because that file is the operator's and holds their provider "
                "configuration. Crew's one channel into this harness today carries a "
                "single environment variable, so a hooks projection needs a channel "
                "decision rather than a writer",
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
