"""Projects the agent spec onto Google Antigravity CLI (``agy``).

Antigravity CLI provides tool capabilities via MCP servers. This mirror projects
the agent spec's MCP server declarations onto the ``session/new`` and ``session/load``
``mcpServers`` array, which the in-tree ``agy-acp`` adapter forwards into the session.
"""

from __future__ import annotations

from typing import Any, Collection, Mapping

from kiro_crew.agent_sdk.backends import ACP_BACKEND_AGY
from kiro_crew.providers.mirrors.base import AgentConfigMirror, Concern
from kiro_crew.providers.mirrors.base import Disposition as _D
from kiro_crew.providers.mirrors.base import Ruling, SessionProjection

__all__ = ["AgyMirror", "agy_projection"]


def agy_projection(
    agent: str | None = None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> SessionProjection:
    """The agy projection: MCP servers are withheld so params has empty mcpServers."""
    return SessionProjection(params={"mcpServers": []})


class AgyMirror(AgentConfigMirror):
    """Projects the agent spec onto Google Antigravity CLI (``agy``)."""

    backend = ACP_BACKEND_AGY

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new and session/load mcpServers array, translated onto "
                "the wire parameters for the in-tree agy-acp adapter",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the harness does not translate a tool allowlist into its session configuration",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.WITHHELD,
                "the harness does not translate denied tools into its session configuration",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "via session/set_model and session/set_config_option('model', ...), "
                "applied to agy's --model argument or runtime configuration",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the harness selects models natively through agy or via Kiro Crew's "
                "model selector, so no second allowlist is written",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "auto-approval is managed by Kiro Crew's PreToolUse gate and agy's "
                "own execution flags rather than projected into the agent spec",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "permission mode is governed by the gateway and CLI stream-json protocol",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "instructions arrive as the turn prompt content rather than being "
                "baked into a static spec",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "the array carries servers rather than raw resource definitions",
            ),
            Concern.HOOKS: Ruling(
                _D.NO_CHANNEL,
                "the ACP session/new element set has no hooks field; Crew's own hooks "
                "fire at the gateway layer",
                channel="a hooks field on the session/new element set",
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
        return agy_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
        )
