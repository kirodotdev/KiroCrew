"""Projects the agent spec onto ``copilot --acp --stdio`` (GitHub Copilot CLI).

The channel is the ``session/new`` / ``session/load`` ``mcpServers`` array: Copilot
CLI reads no Kiro agent spec, so the array is the only route for Crew's own tools,
the spec's servers and a member session's own mounts. Copilot CLI 1.0.100 serves only
the array's ``http``/``sse`` elements from the wire and drops a stdio one, so the
spawn writes the array's stdio half to the ``--additional-mcp-config`` launch file
(``acp.harness.copilot``), where it is measured end to end
(``test/fixtures/acp_frames/copilot/mcp-stdio-mount-live.jsonl``): the server is
started and asked ``initialize``, ``tools/list`` and ``tools/call`` by the harness
itself. Both channels carry the same composition. So the array's MECHANICS are the sibling single-binary harnesses' -- the
shared translation, the ``tools`` allowlist, the control-plane re-derivation and the
one-owner rule for the pooled stubs -- through
:func:`~kiro_crew.providers.mirrors.opencode.place_single_binary_array` as goose and
deepseek do.

Per-tool restrictions are honoured by withholding the narrowed server whole, the
conservative choice goose and deepseek make: no per-call deny path is verified for
this harness.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_COPILOT
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

__all__ = ["CopilotMirror", "copilot_projection"]


def copilot_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The whole copilot array -- spec translation AND pooled stubs.

    The sibling harnesses' projection verbatim, for the reason goose gives: one owner
    for both halves of the array, a server narrowed per tool withheld whole, and Crew's
    own control plane withheld on the same terms. Named here so the day copilot gains
    a per-call deny channel this is the one function that changes.

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
        label="copilot",
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


class CopilotMirror(AgentConfigMirror):
    """Projects the agent spec onto ``copilot --acp --stdio``."""

    backend = ACP_BACKEND_COPILOT

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with no new translator; its stdio "
                "half is mounted from the --additional-mcp-config launch file, since "
                "the harness drops a stdio element sent on the wire. Measured end to end "
                "(mcp-stdio-mount-live.jsonl: mounted, initialize, tools/list and "
                "tools/call asked by copilot itself)",
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
                "by the model choice the harness itself advertises on session/new, "
                "after session/new, not from this array. Copilot CLI 1.0.100 advertised "
                "no model select on the recording account (Auto only on its plan), so "
                "the picker offers auto and the session runs the harness's own choice",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the harness advertises its own model vocabulary on session/new, so a "
                "Crew-side list would be a second answer to a question the session "
                "already answers",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "every tool call asks Crew (allow_all=off is written before the first "
                "prompt); no per-tool approval value is projected into the harness, and "
                "the member's Capabilities pane reports a saved auto-approval as "
                "unverified rather than as applied",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "not projected as spec data; the routing is the allow_all session "
                "config option Crew writes itself",
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
        """The wire face: the ``mcpServers`` array for this copilot session.

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
        """The structured face: :func:`copilot_projection`, ``kwargs`` ignored."""
        del kwargs
        return copilot_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
