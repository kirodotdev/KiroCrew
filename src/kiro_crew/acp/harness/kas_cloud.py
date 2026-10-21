"""The KAS relay placing its sessions in a Kiro cloud sandbox (Kiro Cloud Sessions).

Not a backend of its own, and never served by ``harness_for``:
``AcpRuntime.set_cloud_sandbox`` installs it in place of the runtime's
:class:`KasHarness` before the process spawns, for a session the operator bound to
a Kiro Cloud profile. Every cloud-specific answer lives here, so a session on any
other runtime reaches none of it. It answers the per-session seams differently
from KAS and inherits every other one.

KAS relays a ``cloud-sandbox`` session to Kiro's cloud and forwards only the mode,
the target and the repositories, using the ``_meta.kiro`` keys kiro-cli's own
``--cloud`` flag sends (its ACP client's ``newSession`` and ``loadSession``, as
of kiro-cli 2.21.1). Kiro Crew's custom agent, its MCP servers and its steering
never reach that sandbox, and its tools run there, so Kiro Crew's PreToolUse gate
never sees them. The operator opts into exactly that by choosing a Kiro Cloud
profile, so this host sends none of that surface rather than one the sandbox
would silently drop.

It is built per runtime for one session environment instead of being looked up by
backend. It holds the repositories to check out and the in-flight cloud loads whose
replayed approvals must wait for local registration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from kiro_crew.acp.harness.base import (
    NotificationAliases,
    SessionExtras,
    TeardownPolicy,
    UnroutablePermissionRelay,
)
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.types import METHOD_SESSION_LOAD, JsonRpcMessage

if TYPE_CHECKING:
    from kiro_crew.acp.session_handle import AcpSessionHandle

# A replay must not wait forever for local registration. The load itself has its
# own startup budget; this bounds the pending permission independently.
CLOUD_PERMISSION_HOLD_SECS = 60.0

__all__ = ["CLOUD_SANDBOX_TARGET", "CloudResumeHold", "KasCloudSandboxHarness"]

#: The execution target KAS advertises, and is asked for, to run a session in Kiro's cloud.
CLOUD_SANDBOX_TARGET = "cloud-sandbox"


class _PermissionRelay(Protocol):
    _session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]]

    async def _answer_unroutable_permission(
        self, msg: JsonRpcMessage, session_id: str, *, reason: str
    ) -> None: ...


class CloudResumeHold:
    """One cloud load's hold: released with the handle once the load has finished."""

    def __init__(
        self,
        harness: KasCloudSandboxHarness,
        session_id: str,
        registered: asyncio.Future[AcpSessionHandle | None],
    ) -> None:
        self._harness = harness
        self._session_id = session_id
        self._registered = registered

    def settle(self, handle: Any) -> None:
        if self._harness.loading.get(self._session_id) is self._registered:
            self._harness.loading.pop(self._session_id, None)
        if not self._registered.done():
            self._registered.set_result(handle)


def _kiro_capabilities(agent_capabilities: dict[str, Any]) -> dict[str, Any]:
    meta = agent_capabilities.get("_meta")
    kiro = meta.get("kiro") if isinstance(meta, dict) else None
    return kiro if isinstance(kiro, dict) else {}


class KasCloudSandboxHarness(KasHarness):
    """KAS, with every session it starts placed in a Kiro cloud sandbox."""

    def __init__(self, repositories: Iterable[str] = ()) -> None:
        self.repositories: tuple[str, ...] = tuple(str(r) for r in repositories)
        self.loading: dict[str, asyncio.Future[AcpSessionHandle | None]] = {}

    @property
    def teardown(self) -> TeardownPolicy:
        """KAS delete removes the cloud sandbox, not just this client's attachment."""
        return replace(super().teardown, local_only=True)

    def resume_hold(self, session_id: str) -> CloudResumeHold:
        """Hold approvals KAS replays during this load until the handle is registered.

        Cloud-only, and not a harness seam: the provider's cloud start path asks for
        it around its reopen, so no other host's resume takes this step (H13).
        """
        registered: asyncio.Future[AcpSessionHandle | None] = (
            asyncio.get_running_loop().create_future()
        )
        self.loading[session_id] = registered
        return CloudResumeHold(self, session_id, registered)

    def holds_replayed_permission(self, session_id: str) -> bool:
        """A permission request for a session still loading is a replay to hold."""
        return session_id in self.loading

    def unroutable_permission(
        self,
        runtime: UnroutablePermissionRelay,
        msg: Any,
        session_id: str,
        *,
        reason: str,
    ) -> Callable[[], Awaitable[None]]:
        """A replay during a load waits for the handle; anything else is rejected as before."""
        registered = self.loading.get(session_id)
        if registered is None:
            return super().unroutable_permission(runtime, msg, session_id, reason=reason)
        relay = cast(_PermissionRelay, runtime)
        return lambda: self.hold_permission(relay, msg, session_id, registered)

    async def hold_permission(
        self,
        runtime: _PermissionRelay,
        msg: JsonRpcMessage,
        session_id: str,
        registered: asyncio.Future[AcpSessionHandle | None],
    ) -> None:
        """Wait off the reader, then use the normal permission dispatch path.

        The runtime's answer-task cap bounds these waiters; frame size is already
        transport-bounded. Only the exact session being loaded may wait here.
        """
        try:
            handle = await asyncio.wait_for(
                asyncio.shield(registered), timeout=CLOUD_PERMISSION_HOLD_SECS
            )
        except asyncio.TimeoutError:
            handle = None
        if handle is not None and runtime._session_queues.get(session_id) is handle._queue:
            handle._cloud_replayed_permissions.add(msg.id)
            handle._queue.put_nowait(msg)
        else:
            await runtime._answer_unroutable_permission(
                msg, session_id, reason="cloud_load_not_registered"
            )

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
