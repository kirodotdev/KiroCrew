"""Decide whether an MCP server LOOKS safe to stub and share, from evidence.

This module answers one question and nothing else: given what we managed to
observe about a server, do we RECOMMEND that the operator stub it (and, on a
separate axis, share its backend)? It never writes config, never spawns a
process, and never reads a file — callers gather evidence and hand it in.

Why a recommendation and never an automatic switch
--------------------------------------------------
The evidence available is genuinely weaker than proof, and the gap is
structural rather than an implementation shortcut:

* The probe connects as ``clientInfo: {"name": "kirocrew-probe"}`` while the
  gateway connects as ``kirocrew-gateway``. A server that negotiates its
  capabilities from ``clientInfo`` can answer the probe differently from the
  pooled backend — the exact hazard ``backend.Backend`` documents when it
  caches the first stub's ``initialize`` result and replays it to later stubs.
* The MCP base protocol models one client per stdio server process. There is
  no spec field that says "I am safe to share", so no amount of reading a
  conformant server's declarations can produce a proof.

So a verdict is an invitation to a human decision. The one thing this module
treats as conclusive is REFUTATION: when the gateway has already watched a
server behave statefully under sharing, that observation outranks every
declaration, and the recommendation is withdrawn.

Evidence strength, strongest first
----------------------------------
``REFUTED``      the gateway observed per-client behaviour while shared.
``DISQUALIFIED`` a declaration we trust rules sharing out up front.
``DECLARED``     the server advertises the caller-identity extension, i.e. it
                 was written for a pooled backend.
``MEASURED``     nothing objected AND the pre-flight actually provoked this
                 server as two different callers without finding a divergence.
``NO_OBJECTION`` nothing disqualifying was found — the weakest useful verdict,
                 and the one whose wording must stay honest about that.
``UNKNOWN``      not enough was observed to say anything.

Why ``MEASURED`` sits below ``DECLARED`` but above ``NO_OBJECTION``
------------------------------------------------------------------
``kirocrew.caller-identity`` is an extension this project invented; the MCP base
protocol has no field for "I can tell my callers apart", so no third-party server
will ever send it. Reserving the top tier for servers that declare it left every
real server resting at ``NO_OBJECTION`` for ever, and made a pre-flight able to
REFUTE a server but never to record anything in its favour — the measurement could
only ever cost the operator a verdict.

``MEASURED`` is that missing rung: the server was provoked as two distinct callers
and answered the same way, which rules OUT a caller-sensitive handshake.

It does NOT recommend sharing, and that limit is the point rather than caution.
The pre-flight compares the HANDSHAKE — capability shapes, ``protocolVersion``,
``serverInfo``, the read-only listings — and never makes a tool call. A server
whose state is process-global (one browser context, one database connection, one
working directory) replays that handshake identically and still cannot serve two
sessions: on a shared backend one caller reads state another caller wrote. A
declaration and a measurement are therefore claims about different properties —
ISOLATION versus DETERMINISM — and only the first is grounds for co-tenancy. The
ledger cannot backstop the difference either: its codes describe frames the
gateway could not route, not state handed to the wrong session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# The capability an MCP server advertises to say it can tell its callers apart.
# Kept as a literal rather than imported from ``mcp_caller`` so this module has
# no import edge into the runtime; the ratchet test pins the two together.
CALLER_IDENTITY_CAPABILITY = "kirocrew.caller-identity"

# Env names whose VALUE legitimately differs per session and is deliberately
# excluded from ``PoolKey.effective_env_hash`` (see ``hashing.ENV_SCRUB_PREFIXES``).
# Two co-tenants can therefore disagree on the value while sharing one backend,
# so a server that authenticates from these can never be a safe recommendation.
# This list must stay a superset of the hashing module's prefixes; the ratchet
# test fails if hashing grows a prefix this does not cover.
ROTATING_SECRET_ENV_PREFIXES: tuple[str, ...] = ("AWS_SECRET", "AWS_SESSION", "OAUTH")

# Server capabilities that are per-CLIENT state by construction.
#
# ``resources.subscribe`` creates a subscription owned by one client; the
# resulting ``notifications/resources/updated`` carries no request id, so a
# shared backend cannot attribute it and drops it (deny-by-default in
# ``backend._notification_owner``). The subscription silently stops working.
#
# ``logging`` is a client-scoped level set by ``logging/setLevel``; on a shared
# backend the last caller's level wins for everyone.
#
# ``*.listChanged`` is deliberately NOT here: those notifications are global
# broadcasts (``backend._GLOBAL_BROADCAST_NOTIFICATIONS``) and are safe to
# fan out to every attached stub.
_PER_CLIENT_CAPABILITIES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("resources", "subscribe"), "resources_subscribe", "truthy"),
    (("logging",), "logging_level", "present"),
)


class Strength(str, Enum):
    """How much the evidence is worth. Ordered weakest to strongest."""

    UNKNOWN = "unknown"
    NO_OBJECTION = "no_objection"
    MEASURED = "measured"
    DECLARED = "declared"
    DISQUALIFIED = "disqualified"
    REFUTED = "refuted"


@dataclass(frozen=True)
class Reason:
    """One machine-readable ground for a verdict.

    ``code`` is stable and is what the UI translates; ``detail`` carries the
    specific observation (an env name, a capability path) and is NOT
    translated because it is verbatim data from the server or the config.
    """

    code: str
    detail: str = ""


@dataclass(frozen=True)
class ShareEvidence:
    """Everything observed about one server. All fields optional by design.

    A caller that could not observe something leaves it ``None`` rather than
    guessing, which is what keeps ``UNKNOWN`` distinguishable from
    ``NO_OBJECTION``. ``probe_ok=False`` with ``capabilities=None`` means "we
    never got a handshake", not "the server declared nothing".
    """

    name: str
    # Transport: only stdio servers get a stub at all. HTTP/SSE servers are
    # already shareable by nature and are out of scope, not unsafe.
    is_stdio: bool = True
    # The server resolves the calling session from its own PROCESS (env var, pid
    # walk) rather than from the per-call caller block, so one backend can only
    # ever serve one session correctly.
    #
    # Deliberately NOT "is this one of ours". Kiro Crew's own managed servers
    # differ from each other here: ``kirocrew-core`` advertises the
    # caller-identity extension and consumes the injected caller, while
    # ``kirocrew-cron`` does not and still reads process identity. Keying this on
    # authorship disqualified the first for a property only the second has.
    session_bound_by_construction: bool = False
    # Did the handshake succeed? A server we could not start is UNKNOWN.
    probe_ok: bool = False
    # The server's advertised ``capabilities`` object, verbatim.
    capabilities: dict | None = None
    # ``protocolVersion`` the server answered with. Tool annotations only exist
    # from 2025-03-26 onward, so this decides whether their ABSENCE means
    # anything at all.
    protocol_version: str = ""
    # ``annotations`` from each entry of ``tools/list``, in list order. Empty
    # list = the server returned tools but none carried annotations.
    tool_annotations: list[dict] = field(default_factory=list)
    # Whether ``tools/list`` produced at least one tool.
    has_tools: bool = False
    # Env names DECLARED for this server in its config entry. Values are never
    # passed in — only names are needed and values may be secret.
    declared_env_names: tuple[str, ...] = ()
    # Hazards the gateway already observed while this server was shared.
    observed_hazards: tuple[str, ...] = ()
    # Pre-flight outcome. ``None`` = never run, which is NOT the same as "ran
    # and found nothing": a server that has not been provoked yet must not be
    # recommended for sharing on the strength of its own declarations alone.
    preflight_ran: bool | None = None
    # Set only when ``preflight_ran`` is True. Proof the server answers
    # ``initialize`` differently per caller, which a pooled backend cannot serve.
    preflight_caller_sensitive: bool = False


@dataclass(frozen=True)
class ShareVerdict:
    """The answer. ``recommend_stub`` and ``recommend_share`` are separate."""

    name: str
    strength: Strength
    recommend_stub: bool
    recommend_share: bool
    reasons: tuple[Reason, ...]

    def to_dict(self) -> dict:
        return {
            "strength": self.strength.value,
            "recommendStub": self.recommend_stub,
            "recommendShare": self.recommend_share,
            "reasons": [{"code": r.code, "detail": r.detail} for r in self.reasons],
        }


def rotating_secret_env(env_names: tuple[str, ...]) -> tuple[str, ...]:
    """Declared env names whose value is excluded from the pool key."""
    return tuple(
        n for n in env_names if n.upper().startswith(ROTATING_SECRET_ENV_PREFIXES)
    )


def _per_client_capabilities(capabilities: dict) -> list[Reason]:
    """Capabilities that make a server per-client by construction.

    Two detection modes, because a capability and a flag inside one are different
    claims:

    ``truthy`` — the leaf is a FLAG. ``resources: {"subscribe": false}`` is the
    server explicitly saying it does not subscribe, so it must not count against
    it.

    ``present`` — the leaf IS the capability. In MCP an empty object is the
    standard way to advertise a capability that takes no sub-options, so
    ``{"logging": {}}`` means ``logging/setLevel`` is supported. Testing
    truthiness there let a server whose log level is process-wide — one
    co-tenant's ``setLevel`` changing what every other session gets — through as
    shareable.
    """
    out: list[Reason] = []
    for path, code, mode in _PER_CLIENT_CAPABILITIES:
        node: object = capabilities
        missing = False
        for key in path:
            if not isinstance(node, dict) or key not in node:
                missing = True
                break
            node = node[key]
        if missing:
            continue
        if mode == "present" or node:
            out.append(Reason("per_client_capability", code))
    return out


def _declares_caller_identity(capabilities: dict) -> bool:
    experimental = capabilities.get("experimental")
    return isinstance(experimental, dict) and CALLER_IDENTITY_CAPABILITY in experimental


def _all_tools_read_only(annotations: list[dict]) -> bool:
    """True when every tool declares ``readOnlyHint``.

    Positive evidence only. A server that predates MCP 2025-03-26 cannot send
    annotations at all, so an empty list proves nothing and this returns False
    without that counting against the server.
    """
    if not annotations:
        return False
    return all(a.get("readOnlyHint") is True for a in annotations)


def assess(evidence: ShareEvidence) -> ShareVerdict:
    """Turn evidence into a verdict. Pure; no IO, no config, no clock."""

    def verdict(
        strength: Strength,
        reasons: list[Reason],
        *,
        stub: bool = False,
        share: bool = False,
    ) -> ShareVerdict:
        return ShareVerdict(
            name=evidence.name,
            strength=strength,
            recommend_stub=stub,
            recommend_share=share,
            reasons=tuple(reasons),
        )

    # 1. Refutation outranks every declaration. The gateway does not log these
    #    speculatively: each one is a request it could not route, or a backend
    #    it recycled, while this server was actually shared.
    if evidence.observed_hazards:
        return verdict(
            Strength.REFUTED,
            [Reason("observed_hazard", h) for h in evidence.observed_hazards],
        )

    # 2. Structural exclusions. None of these mean "unsafe" in the same sense —
    #    they mean the question does not apply — so they are reported with
    #    distinct codes rather than one "unsupported".
    if not evidence.is_stdio:
        return verdict(Strength.DISQUALIFIED, [Reason("not_stdio")])
    if evidence.session_bound_by_construction:
        return verdict(Strength.DISQUALIFIED, [Reason("first_party_session_scoped")])

    rotating = rotating_secret_env(evidence.declared_env_names)
    if rotating:
        # Checked BEFORE probe_ok: this is a config fact, true whether or not
        # the server ever started, and reporting it is more useful than
        # "unknown" on a host where the probe cannot run.
        return verdict(
            Strength.DISQUALIFIED,
            [Reason("rotating_secret_env", n) for n in rotating],
        )

    # 3. Nothing observed. Distinguish "never handshook" from "declared nothing".
    if not evidence.probe_ok or evidence.capabilities is None:
        return verdict(Strength.UNKNOWN, [Reason("not_probed")])

    objections = _per_client_capabilities(evidence.capabilities)
    if objections:
        return verdict(Strength.DISQUALIFIED, objections)

    # 4. Pre-flight proof. Checked BEFORE the positive declaration below: a
    #    server may advertise caller-identity and still have been caught
    #    answering `initialize` differently per caller, and the measurement wins
    #    over the promise — the same ordering the hazard ledger enforces.
    if evidence.preflight_ran and evidence.preflight_caller_sensitive:
        return verdict(Strength.DISQUALIFIED, [Reason("caller_sensitive_initialize")])

    # 5. Positive declaration: the server was written for a pooled backend.
    if _declares_caller_identity(evidence.capabilities):
        reasons = [Reason("declares_caller_identity")]
        if _all_tools_read_only(evidence.tool_annotations):
            reasons.append(Reason("all_tools_read_only"))
        # Sharing still needs the provocation to have actually happened. A
        # declaration plus an unrun pre-flight is a promise nobody has tested.
        share = evidence.preflight_ran is True
        reasons.append(
            Reason("preflight_passed" if share else "preflight_not_run")
        )
        return verdict(Strength.DECLARED, reasons, stub=True, share=share)

    # 6. Nothing objected. Which of the two remaining tiers this is depends on
    #    whether anybody actually provoked the server:
    #
    #    * the pre-flight ran and found no divergence -> MEASURED. Something was
    #      ruled OUT: this server does not answer the handshake per caller.
    #    * it never ran -> NO_OBJECTION, an absence of evidence rather than
    #      evidence of absence.
    #
    #    NEITHER recommends sharing, and the reason is the same for both: what
    #    the pre-flight compares is the HANDSHAKE (``initialize`` capability
    #    shapes, ``protocolVersion``, ``serverInfo``, and the read-only listings).
    #    It never makes a tool call. A server whose state is process-global -- one
    #    browser context, one database connection, one working directory -- replays
    #    that handshake identically for two callers and still cannot serve two
    #    sessions, and on a shared backend one caller would receive state another
    #    caller put there.
    #
    #    That is why MEASURED does NOT inherit DECLARED's share flag even though
    #    it sits directly below it: a declaration is a claim about ISOLATION ("I
    #    can tell my callers apart"), while a measurement is a fact about
    #    DETERMINISM ("you answered the same twice"). Conflating the two would let
    #    a bulk action co-tenant a stateful server on evidence that cannot see the
    #    hazard -- and the ledger cannot catch it afterwards either, because its
    #    codes describe unroutable frames, not state a server handed to the wrong
    #    session.
    #
    #    What both tiers DO recommend is the stub, which keeps the backend 1:1
    #    with the session (same topology as no gateway) and is what unlocks
    #    server-authored UI. Splitting stub from share is what makes a verdict
    #    short of a declaration still actionable without being reckless.
    reasons = [Reason("preflight_passed" if evidence.preflight_ran else "no_objection_found")]
    if _all_tools_read_only(evidence.tool_annotations):
        reasons.append(Reason("all_tools_read_only"))
    elif not evidence.tool_annotations:
        reasons.append(Reason("no_tool_annotations", evidence.protocol_version))
    if not evidence.has_tools:
        reasons.append(Reason("no_tools_listed"))
    if evidence.preflight_ran:
        return verdict(Strength.MEASURED, reasons, stub=True, share=False)
    return verdict(Strength.NO_OBJECTION, reasons, stub=True, share=False)
