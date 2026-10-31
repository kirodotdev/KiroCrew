"""Projects the agent spec onto the Strands CLI's Python-project ACP server.

The composition is the sibling single-binary harnesses' -- the shared translation,
the ``tools`` allowlist, the control-plane re-derivation and the one-owner rule for
the pooled stubs, through
:func:`~kiro_crew.providers.mirrors.opencode.place_single_binary_array` -- and none of
it travels on the wire: the CLI refuses a non-empty ``session/new`` ``mcpServers``
array in project mode, so the strands launch writes the whole array to its
``--mcp-config`` file (``acp.harness.strands``), where the CLI mounts it at start.
Measured end to end in ``test/fixtures/acp_frames/strands/mcp-stdio-mount-live.jsonl``.

Per-tool restrictions are honoured by withholding the narrowed server whole, the
conservative choice goose, deepseek and copilot make.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_STRANDS
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
)
from kiro_crew.providers.mirrors.base import Disposition as _D
from kiro_crew.providers.mirrors.base import (
    Ruling,
    SessionProjection,
)
from kiro_crew.providers.mirrors.opencode import place_single_binary_array

__all__ = ["StrandsMirror", "strands_projection"]


def strands_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The whole strands array -- spec translation AND pooled stubs.

    The sibling harnesses' projection verbatim, for the reason goose gives: one owner
    for both halves of the array, a server narrowed per tool withheld whole, and Crew's
    own control plane withheld on the same terms.

    Nothing is caught here, as in goose: an unreadable spec already reads as no spec
    inside the projection, so the one thing that raises is the freshness gate's
    ``DerivedSpecStale``, and that has to reach the spawn so it is refused.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    narrowed = frozenset(server for server, _tool in projection.disabled_tools)
    out = place_single_binary_array(
        projection,
        label="strands",
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
        agent_spec=projection.agent_spec,
    )


class StrandsMirror(AgentConfigMirror):
    """Projects the agent spec onto the Strands CLI's project-mode ACP server."""

    backend = ACP_BACKEND_STRANDS

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session's projected mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with no new translator, written to "
                "the --mcp-config launch file the Strands CLI mounts at start (the CLI "
                "refuses a non-empty session/new array in project mode). Measured end to "
                "end (mcp-stdio-mount-live.jsonl)",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "into the allowlist that decides which servers enter the array -- the "
                "shared translation's own rule, not a second one here",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "by withholding the server it narrows, Crew's own control plane "
                "included, declared as per_tool_deny=whole-server",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "as the CLI's --model launch argument; auto launches with the configured "
                "Strands model (agent.strands_provider / agent.strands_model; 'auto' "
                "passes no --model, so the provider's CLI default runs)",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the CLI resolves provider/model ids itself and advertises no list on "
                "session/new, so a Crew-side list would gate nothing",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "every tool call asks Crew through the gate module's interrupt; no "
                "per-tool approval value is projected into the harness",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "not projected as spec data; the routing is the gate module Crew loads",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "no definition is projected; the session's instructions arrive as the "
                "turn's own text",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "no channel is advertised for them, and the array carries servers rather "
                "than resources",
            ),
            Concern.HOOKS: Ruling(
                _D.WITHHELD,
                "this backend is not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS, so a saved "
                "hooks block is reported as a projection gap rather than claimed",
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
        """The wire face: the ``mcpServers`` array for this strands session.

        ``permission_surface_owned`` arrives in ``kwargs`` and is IGNORED, as for every
        mirror outside claude's class: there is no Crew-owned permission file in play.
        Blocking -- it reads the agent spec.
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
        """The structured face: :func:`strands_projection`, ``kwargs`` ignored."""
        del kwargs
        return strands_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
