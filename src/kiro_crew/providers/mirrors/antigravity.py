"""Project the agent spec onto Google Antigravity's ACP session.

Antigravity reads the ACP ``mcpServers`` array and no Kiro agent file. Its
single-binary array mechanics match goose and Copilot, so this mirror delegates
to the same placement function and keeps Antigravity's own rulings beside it.

The runtime was driven far enough against 1.4.4 to establish the transport: a
stdio server on ``session/new`` was started and asked ``server/discover`` and
``tools/list``. A completed model turn requires an authenticated Google account
and is not claimed here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_ANTIGRAVITY
from kiro_crew.providers.mirrors.base import AgentConfigMirror, Concern
from kiro_crew.providers.mirrors.base import Disposition as _D
from kiro_crew.providers.mirrors.base import Ruling, SessionProjection
from kiro_crew.providers.mirrors.opencode import place_single_binary_array

__all__ = ["AntigravityMirror", "antigravity_projection"]


def antigravity_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """Build the complete MCP array for one Antigravity session.

    A server narrowed per tool is withheld whole. Antigravity's permission request
    does carry MCP identity, but that identity has not been captured through a
    completed authenticated tool call, so this projection deliberately leaves no
    switched-off tool reachable on the strength of an unverified approval shape.

    Blocking (parses the agent spec once), so callers use it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    narrowed = frozenset(server for server, _tool in projection.disabled_tools)
    out = place_single_binary_array(
        projection,
        label="antigravity",
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


class AntigravityMirror(AgentConfigMirror):
    """Projects the agent spec onto ``agy_acp_server``."""

    backend = ACP_BACKEND_ANTIGRAVITY

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new and session/load mcpServers array, translated by the "
                "shared session-MCP projection and placed by the same function used "
                "for goose and Copilot. Driven on Antigravity ACP 1.4.4: a stdio "
                "server on the array was started and asked server/discover and "
                "tools/list by the Antigravity client",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "into the shared server allowlist that decides which MCP servers enter "
                "the array; native-tool restrictions continue through Crew's permission "
                "gate rather than being projected into a second Antigravity setting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "by withholding the whole server named by a disabledTools entry, Crew's "
                "control plane included, and declaring per_tool_deny=whole-server. This "
                "is conservative until a live authenticated permission request confirms "
                "the stable MCP identity a narrower per-call refusal would require",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "through the model config option Antigravity advertises on session/new "
                "and session/load, rather than through this MCP array",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "Antigravity advertises its own model vocabulary on the session; a "
                "Crew-side list would be a second and potentially stale answer",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "no per-tool auto-approval is projected. Agent specs that carry one are "
                "reported as unverified rather than weakening Crew's approval gate",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "not projected as spec data. Crew owns the mode through the required "
                "session config write mode=default, and refuses auto_edit and yolo",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "no agent definition is written for Antigravity; the session's "
                "instructions arrive in the turn's own text",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "the session MCP array carries servers rather than resources, and no "
                "resource channel is projected by this adapter",
            ),
            Concern.HOOKS: Ruling(
                _D.TRANSLATED,
                "the ACP session array has no hooks field, so Antigravity never receives "
                "the spec's hooks. Crew runs them at each permission request; native "
                "calls are mapped by their ACP tool kind and MCP calls by Antigravity's "
                "trusted _meta mcp identity",
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
        del kwargs
        return antigravity_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
