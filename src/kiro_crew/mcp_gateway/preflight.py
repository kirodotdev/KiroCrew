"""Provoke, before any user is served, the failure that sharing would cause.

Waiting for a shared backend to misbehave means a real session pays: the
affected chat loses that server's tools until it is reopened. This module
anticipates instead. It asks the question the pool will ask later — "does this
server behave the same for every caller?" — while nobody is attached.

The check that matters, and why it is decidable
----------------------------------------------
``Backend`` caches the first stub's ``initialize`` result and replays it to every
later stub, and its own docstring says the MCP spec does not require a server to
answer ``initialize`` the same way twice: a server that negotiates capabilities
from ``clientInfo`` would silently hand session B session A's capability set.
That is documented there as an assumption the gateway cannot verify at runtime.

It IS verifiable offline. Spawn the server twice under two different
``clientInfo`` identities and compare what it advertises. Divergence is proof of
caller sensitivity; agreement is strong evidence against it. Two sequential
spawns, no broker, no synthetic co-tenancy.

What this does NOT catch, on purpose
------------------------------------
State that only appears after real work (a server that starts keying on the
caller only once someone authenticates), and behaviour that needs genuine
concurrency. Those stay the ``hazards`` ledger's job — which is why the ledger
outranks anything decided here rather than being a fallback for it.

Cost
----
Two spawns per evaluated server, and only for servers whose execution identity
changed (see ``verdict_cache``). Never on the request path: probing spawns
processes, and this module is called from an explicit action or a background
pass, never while rendering a page.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from kiro_crew.mcp_discovery import probe_server

logger = logging.getLogger(__name__)

#: Two identities that differ in every field a server could branch on. Distinct
#: names AND versions, so a server keying on either is caught.
#:
#: Three constraints on the pair, all load-bearing:
#:
#: * Neither may be a name a real product owns. A probe that presents itself as
#:   somebody else's tool is misattributed in that server's own logs and policy,
#:   and it measures the wrong thing: a server that unlocks diagnostic
#:   capabilities for a debugging client would read as caller-sensitive on a
#:   difference the gateway will never provoke.
#: * They must share no substring a server would plausibly key on — not the
#:   vendor name, not ``preflight``. Two identities that both look like us would
#:   answer alike at a server that personalises per vendor, and the divergence
#:   would go unseen.
#: * The names are imported by the real-server test, which branches on them by
#:   equality. Renaming one here without updating that fixture makes the fixture
#:   stop diverging; keep them exported rather than duplicated.
_IDENTITY_A: dict[str, str] = {"name": "kirocrew-preflight-a", "version": "1.0.0"}
_IDENTITY_B: dict[str, str] = {"name": "mcp-client-b", "version": "9.9.9"}

#: The pair, for the real-server test to branch on by equality.
PREFLIGHT_IDENTITY_NAMES: tuple[str, str] = (_IDENTITY_A["name"], _IDENTITY_B["name"])

#: Reason codes this module can contribute. Kept here (not in ``shareability``)
#: because they describe how the evidence was OBTAINED, and the verdict engine
#: only needs to know they are disqualifying.
REASON_CALLER_SENSITIVE_INIT = "caller_sensitive_initialize"
REASON_PREFLIGHT_UNAVAILABLE = "preflight_unavailable"


@dataclass(frozen=True)
class PreflightResult:
    """What the pre-flight learned. ``ran`` separates "no" from "could not ask".

    A server that refuses to start in this environment — needs a credential, a
    tunnel, a display — is NOT a server that failed the check, and collapsing
    the two would silently mark half a fleet unshareable. Callers must branch on
    ``ran`` before reading ``caller_sensitive``.
    """

    ran: bool
    caller_sensitive: bool = False
    #: Verbatim capability objects from each identity, kept for the diagnostic
    #: surface. Never emitted as telemetry — they are free-form server data.
    capabilities_a: dict[str, Any] | None = None
    capabilities_b: dict[str, Any] | None = None
    detail: str = ""

    @property
    def reasons(self) -> tuple[str, ...]:
        if not self.ran:
            return (REASON_PREFLIGHT_UNAVAILABLE,)
        if self.caller_sensitive:
            return (REASON_CALLER_SENSITIVE_INIT,)
        return ()


def _capability_shape(capabilities: dict[str, Any] | None) -> Any:
    """A comparable projection of an advertised capability object.

    Compares the SHAPE — which capability keys exist and which flags are on —
    rather than the whole object, because a conformant server may legitimately
    vary free-form ``experimental`` payloads (a build id, a session token) that
    say nothing about caller sensitivity. Comparing raw dicts would report every
    such server as caller-sensitive and the check would be useless.
    """
    if capabilities is None:
        return None

    def project(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: project(v) for k, v in sorted(node.items())}
        if isinstance(node, bool):
            return node
        # Any other leaf collapses to a presence marker: its VALUE is not part
        # of the contract a pooled backend has to keep identical.
        return "*"

    return project(capabilities)


async def preflight(server: Any) -> PreflightResult:
    """Run the shareability pre-flight for one configured server.

    *server* is an ``McpServerInfo``. It is NOT mutated: this function probes
    copies, so a pre-flight can never overwrite the status or tool list the
    dashboard is showing.
    """

    async def ask(identity: dict[str, str]) -> Any:
        probe = deepcopy(server)
        await probe_server(probe, client_info=identity)
        return probe

    try:
        first = await ask(_IDENTITY_A)
    except Exception as exc:  # pragma: no cover - defensive; probe owns its errors
        logger.debug("preflight %s: first handshake raised: %s", server.name, exc)
        return PreflightResult(ran=False, detail="probe_error")

    if first.status != "ok":
        # Could not start it. Says nothing about shareability.
        return PreflightResult(ran=False, detail=first.error or first.status)

    try:
        second = await ask(_IDENTITY_B)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("preflight %s: second handshake raised: %s", server.name, exc)
        return PreflightResult(ran=False, detail="probe_error")

    if second.status != "ok":
        # It answered once and not twice. That is a flaky or single-shot server,
        # not a proven caller-sensitive one — and pooling it would be a bad idea
        # for a different reason, so report "could not ask" rather than inventing
        # a verdict this evidence does not support.
        return PreflightResult(ran=False, detail=second.error or second.status)

    shape_a = _capability_shape(first.capabilities)
    shape_b = _capability_shape(second.capabilities)
    diverged = shape_a != shape_b
    if diverged:
        logger.info(
            "preflight %s: advertises different capabilities per clientInfo; "
            "not shareable",
            server.name,
        )
    return PreflightResult(
        ran=True,
        caller_sensitive=diverged,
        capabilities_a=first.capabilities,
        capabilities_b=second.capabilities,
        detail="capability_shape_differs" if diverged else "",
    )
