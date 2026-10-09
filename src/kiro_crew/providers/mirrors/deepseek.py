"""Projects the agent spec onto ``dsh --profile acp`` (DeepSeek).

Until this mirror, a deepseek session carried only Crew's pooled broker stubs: the
registry recorded it as BROKER_ONLY, so the servers an agent spec declares, and the
spec's per-tool restrictions, never reached it. A member on this backend therefore had
no saved spec to confirm, and could not be offered as a member's execution seat.

The channel is the one already measured for the stubs
(``test/fixtures/acp_frames/deepseek/mcp-stdio-mount-live.jsonl``): stdio elements in
the ``session/new`` ``mcpServers`` array, mounted and called by the harness itself. So
the array's MECHANICS are the sibling single-binary harnesses' -- the shared
translation, the ``tools`` allowlist, the control-plane re-derivation and the
one-owner rule for the pooled stubs -- through
:func:`~kiro_crew.providers.mirrors.opencode.place_single_binary_array` as goose does.

What is deepseek's, and changes no projection rule:

* An element whose command cannot start fails ``session/new`` WHOLE
  (``mcp-stdio-rollback-live.jsonl``) rather than being dropped, so a spec server that
  cannot launch refuses the session instead of degrading it. That is loud, which is
  the right direction for a member whose saved tools are the point.
* Per-tool restrictions are honoured by withholding the narrowed server whole, the
  conservative choice goose makes: no per-call deny path is verified for this harness.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_DEEPSEEK
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

__all__ = ["DeepSeekMirror", "deepseek_projection"]


def deepseek_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The whole deepseek array -- spec translation AND pooled stubs.

    The sibling harnesses' projection verbatim, for the reason goose gives: one owner
    for both halves of the array, a server narrowed per tool withheld whole, and Crew's
    own control plane withheld on the same terms. Named here so the day deepseek gains
    a per-call deny channel this is the one function that changes.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    narrowed = frozenset(server for server, _tool in projection.disabled_tools)
    out = place_single_binary_array(
        projection,
        label="deepseek",
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


class DeepSeekMirror(AgentConfigMirror):
    """Projects the agent spec onto ``dsh --profile acp``."""

    backend = ACP_BACKEND_DEEPSEEK

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with no new translator. The stdio "
                "element shape is measured end to end for this harness "
                "(mcp-stdio-mount-live.jsonl: mounted, initialize, tools/list and "
                "tools/call asked by dsh itself). An element that cannot start fails "
                "the whole session rather than being dropped",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "into the allowlist that decides which servers enter the array -- the "
                "shared translation's own rule, not a second one here",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "by withholding the server it narrows, Crew's own control plane "
                "included, declared as per_tool_deny=whole-server. Conservative: no "
                "per-call deny path is verified for this harness, and withholding the "
                "whole server is the direction that cannot leave a switched-off tool "
                "reachable",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "by the lane's ACP profile (KIROCREW_DSH_MODEL composed before "
                "session/new), not from this array",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the harness advertises its own model vocabulary on session/new, so a "
                "Crew-side list would be a second answer to a question the session "
                "already answers",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "dsh's own sandbox decides its tool calls; no per-tool approval value "
                "is projected, and the member's Capabilities pane reports a saved "
                "auto-approval as unverified rather than as applied",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "not projected as spec data; the routing is the lane profile's",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "no definition is projected in any shape; the session's instructions "
                "arrive as the turn's own text",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "no channel is advertised for them, and the array carries servers rather "
                "than resources",
            ),
            Concern.HOOKS: Ruling(
                _D.WITHHELD,
                "the session/new element set has no hooks field and this backend is not "
                "in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS, so a saved hooks block is reported "
                "as a projection gap rather than claimed",
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
        """The wire face: the ``mcpServers`` array for this deepseek session.

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
        """The structured face: :func:`deepseek_projection`, ``kwargs`` ignored."""
        del kwargs
        return deepseek_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
