"""Projects the agent spec onto Kiro Crew's OWNED LM Studio ACP adapter.

The adapter is a thin ACP<->OpenAI translation in front of a LOCAL LM Studio server
(``python -m kiro_crew.acp.lmstudio_server``). It reads no
``~/.kiro/agents/<name>.json``, so the ``session/new`` + ``session/load``
``mcpServers`` array is the only channel Crew's own tools reach it by -- and the
translation is the SHARED one (:func:`kiro_crew.acp.session_mcp.session_mcp_projection`,
no new translator), with the same ``tools`` allowlist, registry filter and
one-owner rule for the pooled broker stubs the sibling spec harnesses use.

What is this backend's own is the per-tool restriction rule, and it is the reason
the projection carries a deny set rather than only an array. This adapter gates
every built-in and bridged MCP call through Crew's fail-closed permission relay, so
the CLIENT is the enforcer and it needs the same ``(server, tool)`` pairs the parse
produced: dropping them here would leave a spec's switched-off tools unenforceable
on this family. A narrowed server is therefore withheld WHOLE (the conservative
direction while the array is the only channel Crew has), and the pairs travel
alongside so the client refuses a call the array could not remove.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from kiro_crew.acp.session_mcp import session_mcp_projection
from kiro_crew.agent_sdk.backends import ACP_BACKEND_LMSTUDIO
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
    SessionProjection,
)

_D = Disposition

__all__ = ["LmStudioMirror", "lmstudio_projection"]


def lmstudio_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
) -> SessionProjection:
    """The whole LM Studio array -- spec translation AND pooled stubs.

    The mirror's :meth:`~LmStudioMirror.session_projection`, as a function so it can
    be called and tested without the class.

    ONE owner for both halves of the array, exactly as the sibling harnesses have: a
    pooled stub carries the same name as the spec entry it rewrites, so a stub
    appended after the projection withheld that name would un-withhold it -- and the
    stub is the UNRESTRICTED server. Taking the stub ELEMENTS here, beside the stub
    NAMES the translation already yields to, lets the withhold rule run over both
    halves in one place. A stub is held to the spec's ``tools`` allowlist from the
    same parse that filtered the translated half.

    ``denied_tools`` is carried on the projection because the adapter's client is the
    execution gate -- see the module docstring. It is derived on the SAME parse as the
    array, so it cannot name a tool on a spec revision the array never saw.

    Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    names: Collection[str] = (
        tuple(item for item in stub_server_names if isinstance(item, str))
        if isinstance(stub_server_names, Collection)
        and not isinstance(stub_server_names, (str, bytes))
        else ()
    )
    elements: Collection[Mapping[str, Any]] = (
        tuple(item for item in stub_elements if isinstance(item, Mapping))
        if isinstance(stub_elements, Collection) and not isinstance(stub_elements, (str, bytes))
        else ()
    )
    projection = session_mcp_projection(agent, stub_server_names=names, work_dir=work_dir)
    withheld = projection.restricted | projection.disabled_servers
    servers = [item for item in projection.servers if str(item.get("name") or "") not in withheld]
    claimed = {str(item.get("name")) for item in servers if isinstance(item, Mapping)}
    for stub in elements:
        name = str(stub.get("name") or "")
        if (
            name
            and name not in claimed
            and name not in withheld
            and projection.allowlist.grants(name)
        ):
            servers.append(dict(stub))
            claimed.add(name)
    return SessionProjection(
        params={"mcpServers": servers},
        # Both client obligations travel WITH the array. The direct adapter gates
        # every built-in and bridged MCP call through Crew's fail-closed permission
        # relay, so the client is the enforcer: ``denied_tools`` is the per-tool half,
        # and ``disabled_servers``/``restricted_servers`` are what the array withheld
        # so a caller appending an element of its own cannot put them back.
        denied_tools=projection.disabled_tools,
        disabled_servers=projection.disabled_servers,
        restricted_servers=projection.restricted,
        derived_spec_snapshot=projection.derived_spec_snapshot,
    )


class LmStudioMirror(AgentConfigMirror):
    """Projects the agent spec onto the local LM Studio ACP adapter."""

    backend = ACP_BACKEND_LMSTUDIO

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.TRANSLATED,
                "the session/new + session/load mcpServers array, translated by the "
                "shared acp.session_mcp.session_mcp_projection with NO new translator "
                "and narrowed the way the sibling spec harnesses narrow it. This "
                "adapter reads no agent spec of its own, so the array is the only "
                "channel Crew's tools reach it by",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "into the allowlist that decides which servers enter the array -- the "
                "shared translation's own rule, not a second one here -- and applied to "
                "the pooled broker stubs of the same name",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "the disabled (server, tool) pairs travel to the CLIENT on the "
                "projection (SessionProjection.denied_tools) and the narrowed server is "
                "withheld from the array, because this transport has no per-tool MCP "
                "slot of its own. The adapter asks session/request_permission per MCP "
                "call, so Crew's own client refuses a switched-off tool by the pair it "
                "reports",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "the adapter accepts no native auto-approval; approval is host-side and "
                "routed through Crew's permission relay, so there is no per-tool value "
                "to project",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "as an LM Studio model id on session/new and session/set_model -- the "
                "adapter passes it straight to the local server",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.DELIVERED,
                "the local LM Studio server advertises its live downloaded-model list, "
                "so a Crew-side list would be a second answer to a question the server "
                "already answers",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "permission mode is host-side rather than an LM Studio option, so there "
                "is nothing on this wire to carry it",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "no definition is projected in any shape; the prompt is assembled by the "
                "ACP client and arrives as the turn's own text",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "no channel is advertised for them, and the array carries servers rather "
                "than resources",
            ),
            Concern.HOOKS: Ruling(
                _D.WITHHELD,
                "the session/new element set has no hooks field, and this adapter runs no "
                "hooks of its own; the spec's hooks are host-side and would be fired by "
                "Crew's turn loop on a backend that declares it",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        work_dir: object = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this LM Studio session.

        ``permission_surface_owned`` arrives in ``kwargs`` and is ignored, which is the
        documented behaviour for a mirror outside claude's class: this adapter gates
        every tool call through Crew's relay by construction, so the flag that exists
        for a harness whose permission surface is a file Crew may not own does not
        apply here.

        Blocking -- it reads the agent spec. The caller warms this on the adapter's
        spawn path and serves the shared ``session/new`` call site from that cache
        (harness-parity H13).
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
        ).params

    def session_projection(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        stub_elements: Collection[Mapping[str, Any]] = (),
        work_dir: object = None,
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`lmstudio_projection`, with ``kwargs`` ignored as
        :meth:`session_params` documents. ``denied_tools`` is filled on this backend by
        decision -- see the ``disabledTools`` ruling."""
        del kwargs
        return lmstudio_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
        )
