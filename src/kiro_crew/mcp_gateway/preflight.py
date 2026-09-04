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
``clientInfo`` identities and compare every facet the backend replays: the
advertised capabilities, the negotiated protocol version, the server's own
identification, and the ``tools/list`` answer. Divergence on any of them is
proof of caller sensitivity; agreement is strong evidence against it. Two
sequential spawns, no broker, no synthetic co-tenancy.

Which facet diverged is logged and carried in ``detail`` for diagnosis, but the
verdict is one boolean: every consumer of a stored measurement reduces it to
"caller sensitive or not", so reporting the facet to an operator would be a
change to that whole path rather than to this module.

Why the tool list is part of it
------------------------------
The tool list is mandatory in every revision of the protocol, while tool
ANNOTATIONS only exist from MCP 2025-03-26. Comparing the list is therefore the
one facet that is decidable on a server too old to describe its own tools — the
servers most likely to predate any thought of co-tenancy. The probe already
issues ``tools/list`` and stores the answer, so this costs no extra spawn and no
extra request; the earlier shape of this module simply discarded it.

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

import json
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
#:
#: One divergence code covers every facet, because every consumer collapses
#: the measurement to a single boolean: the row builder keeps ``(ran,
#: caller_sensitive)`` and the verdict engine derives the reason itself. Naming
#: which facet caught the divergence would need that whole path to carry the
#: stored reason, so it is a change of its own rather than a constant declared
#: here with nothing reading it.
#:
#: The VALUE matches the code the verdict engine emits, so a grep for either
#: finds both. It deliberately no longer says "caller sensitive": two spawns that
#: both vary ``clientInfo`` cannot tell a caller-derived answer from one that
#: varies for the server's own reasons, so the honest name describes what was
#: seen rather than what caused it.
REASON_HANDSHAKE_NOT_REPRODUCIBLE = "handshake_not_reproducible"
REASON_PREFLIGHT_UNAVAILABLE = "preflight_unavailable"


def _tool_names_are_comparable(server: Any) -> bool:
    """True when this server's tool names can be compared at all.

    The prober takes a tool's ``name`` straight from the server's ``tools/list``
    and only drops falsy values, so a non-string name (``{"name": 123}``) reaches
    ``server.tools`` intact. Ordering a mixed list raises ``TypeError``, and that
    exception would escape the whole evaluation pass — which flushes only after
    every measurement completes, so one malformed server would discard the
    verdicts of every server measured before it.

    A server that cannot name its own tools is not a server this check can reach a
    conclusion about, so it reads as unmeasurable. That is the same answer the
    module already gives for a server it could not start: honest, and it lands as
    ``unknown`` rather than as evidence against the server.
    """
    return all(isinstance(n, str) for n in (getattr(server, "tools", None) or []))


#: The facets compared across the two identities. Their NAMES are internal: they
#: order the comparison and appear in the log line that says which one diverged.
FACET_INIT = "initialize"
FACET_TOOLS = "tools"


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
            return (REASON_HANDSHAKE_NOT_REPRODUCIBLE,)
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


def _tool_surface(server: Any) -> Any:
    """What ``tools/list`` answered, projected so only real divergence shows.

    The probe already pays for this round-trip and stores the result, so widening
    the comparison here costs no extra spawn and no extra request. It is what
    makes the check decidable on a server that sends no annotations at all: the
    tool LIST is mandatory in every MCP revision, so a server too old to carry
    ``readOnlyHint`` is still fully measurable on this facet.

    Two projections keep ordering out of the comparison, because a pooled backend
    replays the cached answer and a server free to enumerate its tools in a
    different order each spawn is not thereby caller-sensitive:

    * names become a SET — a different set is a real divergence, a different
      order is not;
    * annotation shapes become a sorted MULTISET, never paired with a name.

    That second projection is deliberately unpaired, and this is the load-bearing
    part. The prober builds ``tools`` and ``tool_annotations`` in two comprehensions
    over the same list with INDEPENDENT predicates — a tool is kept in ``tools``
    when its name is truthy, and in ``tool_annotations`` when it carries an
    ``annotations`` dict. Neither list records which tool an entry came from, so a
    tool with an empty name but an annotation object is dropped from one and kept in
    the other: the two lists can be the same LENGTH and still describe different
    tools. Any attempt to re-establish alignment on this side of the wire is
    therefore guessing, whether it pairs by index or first checks the lengths.

    So the alignment question is removed rather than answered. Comparing the
    shapes as an unordered multiset still detects the thing this facet exists to
    detect — a server that makes a different annotation claim to the second caller
    changes the multiset — while a reorder, a partial list, and a mismatched filter
    all become unobservable instead of becoming a wrong verdict. Per-tool
    attribution is not lost, because nothing consumed it: one reason code covers
    every facet, and no caller reads which tool diverged.

    Each shape is canonicalised to a JSON string before sorting. The projection
    yields dicts, and ordering dicts directly would raise ``TypeError`` — the same
    crash class this module already guards against for tool names.

    Annotation VALUES are compared through the same shape projection as
    capabilities: what matters is that the server made the same claim to both
    callers, and a free-form leaf it varies per spawn is not that claim.
    """
    names = list(getattr(server, "tools", None) or [])
    annotations = list(getattr(server, "tool_annotations", None) or [])
    shapes = sorted(
        json.dumps(
            _capability_shape(ann if isinstance(ann, dict) else None),
            sort_keys=True,
        )
        for ann in annotations
    )
    return (tuple(sorted(names)), tuple(shapes))


def _replayed_surface(server: Any) -> dict[str, Any]:
    """Every facet a pooled backend caches from one caller and serves to the next.

    ``Backend`` replays the first stub's handshake AND its tool list, so each of
    these is a promise the server has to keep identically for co-tenancy to be
    safe. Anything the backend does NOT replay is deliberately absent: comparing
    it would fail servers on differences no session could ever observe.
    """
    return {
        FACET_INIT: (
            _capability_shape(getattr(server, "capabilities", None)),
            getattr(server, "protocol_version", "") or "",
            _capability_shape(getattr(server, "server_info", None) or None),
        ),
        FACET_TOOLS: _tool_surface(server),
    }


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

    # Checked BEFORE any comparison, and against BOTH answers: the comparison
    # orders tool names, so a non-string name would raise out of this function and
    # abort the caller's whole pass before it flushes anything.
    if not (_tool_names_are_comparable(first) and _tool_names_are_comparable(second)):
        logger.info("preflight %s: tool names are not all strings; unmeasurable", server.name)
        return PreflightResult(ran=False, detail="malformed_tool_names")

    surface_a = _replayed_surface(first)
    surface_b = _replayed_surface(second)
    # Ordered so the reported facet is stable rather than dict-iteration
    # dependent, and so the handshake is named ahead of the tool list when a
    # server diverges on both — the handshake is the earlier promise.
    diverged_facet = next(
        (f for f in (FACET_INIT, FACET_TOOLS) if surface_a[f] != surface_b[f]), ""
    )
    if diverged_facet:
        logger.info(
            "preflight %s: %s differs per clientInfo; not shareable",
            server.name,
            diverged_facet,
        )
    return PreflightResult(
        ran=True,
        caller_sensitive=bool(diverged_facet),
        capabilities_a=first.capabilities,
        capabilities_b=second.capabilities,
        detail=f"{diverged_facet}_shape_differs" if diverged_facet else "",
    )
