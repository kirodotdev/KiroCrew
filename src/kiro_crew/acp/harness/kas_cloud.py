"""The KAS relay placing its sessions in a Kiro cloud sandbox (Kiro Cloud Sessions).

Not a backend of its own, and never served by ``harness_for``:
``AcpRuntime.set_cloud_sandbox`` installs it in place of the runtime's
:class:`KasHarness` before the process spawns, for a session the operator bound to
a Kiro Cloud profile. Every cloud-specific answer lives here, so a session on any
other runtime reaches none of it. It answers the per-session seams differently
from KAS and inherits every other one.

KAS relays a ``cloud-sandbox`` session to Kiro's cloud and forwards only the mode,
the target and the repositories, using the ``_meta.kiro`` keys kiro-cli's own
``--cloud`` flag sends. Kiro Crew's custom agent, its MCP servers and its steering
never reach that sandbox, and its tools run there, so Kiro Crew's PreToolUse gate
never sees them. The operator opts into exactly that by choosing a Kiro Cloud
profile, so this host sends none of that surface rather than one the sandbox
would silently drop.

It holds one value beyond its backend id, the repositories to check out, because
it is built per runtime for one session environment instead of being looked up by
backend.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from kiro_crew.acp.harness.base import NotificationAliases, SessionExtras
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.types import METHOD_SESSION_LOAD

__all__ = ["CLOUD_SANDBOX_TARGET", "KasCloudSandboxHarness"]

#: The execution target KAS advertises, and is asked for, to run a session in Kiro's cloud.
CLOUD_SANDBOX_TARGET = "cloud-sandbox"


def _kiro_capabilities(agent_capabilities: dict[str, Any]) -> dict[str, Any]:
    meta = agent_capabilities.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else {}


class KasCloudSandboxHarness(KasHarness):
    """KAS, with every session it starts placed in a Kiro cloud sandbox."""

    def __init__(self, repositories: Iterable[str] = ()) -> None:
        self.repositories: tuple[str, ...] = tuple(str(r) for r in repositories)

    async def session_extras(
        self,
        agent: str,
        *,
        work_dir: str | Path | None,
        mcp_gateway_overlay: Any = None,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
    ) -> SessionExtras:
        """No agent definitions: the sandbox runs Kiro's own default agent."""
        return SessionExtras()

    def session_mcp_servers(
        self,
        requested: list[dict[str, Any]],
        *,
        agent_capabilities: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """No servers: none of Kiro Crew's servers would run in the sandbox."""
        return []

    @property
    def notification_aliases(self) -> NotificationAliases:
        """KAS's vocabulary without the readiness barrier.

        No server was sent, so none can report, and a barrier would only wait out
        its ceiling. Together with the runtime declared MCP-free, the start drains
        with no ceiling at all.
        """
        return replace(super().notification_aliases, mcp_readiness=False)

    def session_request(
        self,
        method: str,
        params: dict[str, Any],
        agent: str | None,
        *,
        agent_capabilities: dict[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """Ask KAS for a cloud sandbox, and activate no Kiro Crew agent there.

        Refuses when KAS did not offer cloud sandboxes to this sign-in, rather than
        let the session run locally. A ``session/load`` reopens the session from
        Kiro's cloud store with nothing but its id and directory.
        """
        kiro = _kiro_capabilities(agent_capabilities)
        targets = kiro.get("executionTargets")
        if not isinstance(targets, list) or CLOUD_SANDBOX_TARGET not in targets:
            from kiro_crew.acp.session_handle import AcpRuntimeError

            raise AcpRuntimeError(
                "Kiro Cloud Sessions are not available to this sign-in: the Kiro agent "
                "server did not offer cloud sandboxes. Sign in to Kiro with an account "
                "that has Kiro Cloud Sessions, or choose another session environment."
            )
        cloud: dict[str, Any] = {}
        sources = kiro.get("sessionSources")
        if isinstance(sources, list) and "remote" in sources:
            cloud["sessionSource"] = "remote"
        if method == METHOD_SESSION_LOAD:
            return (
                {
                    "sessionId": params.get("sessionId"),
                    "cwd": params.get("cwd"),
                    "mcpServers": [],
                    "_meta": {"kiro": cloud},
                },
                None,
            )
        cloud["executionTarget"] = {"kind": CLOUD_SANDBOX_TARGET}
        if self.repositories:
            cloud["repositories"] = list(self.repositories)
        else:
            cloud["isEmptyWorkspace"] = True
        out = dict(params)
        meta = dict(out.get("_meta") or {})
        meta["kiro"] = {**(meta.get("kiro") or {}), **cloud}
        out["_meta"] = meta
        return out, None
