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
from typing import Collection

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

# Server capabilities that DEGRADE when a backend is shared.
#
# Reported as notes and nothing more: they do not disqualify a server and they do
# not withhold a recommendation. Two reasons, and the second is the load-bearing
# one.
#
# First, it is not a leak. The broker already refuses to guess: an unattributable
# request-scoped notification is DROPPED (deny-by-default in
# ``backend._notification_owner``) rather than broadcast, so no co-tenant ever
# receives another tenant's content.
#
# Second, and this is why it informs rather than gates: **it describes a gap in
# OUR broker, not a property of the server.** A proxy that can correlate a frame
# to a caller can route it, and correlation is a feature we can build:
#
# ``logging`` -- ``logging/setLevel`` is process-global, but a proxy can emit at
# the finest level any tenant asked for and filter DOWN per stub, giving each
# tenant the verbosity it requested from one process.
#
# Withholding pooling for it would be charging the operator for work we have
# not done. It is filed as a broker gap instead, and the contract on this table
# is that an entry is DELETED — not relaxed — the moment the broker learns to
# attribute the frames it covers. ``resources.subscribe`` is absent from this
# table under exactly that contract: the broker keeps a ``uri -> {stub_uuid}``
# subscription table (``backend._resource_subscriptions``) and routes each
# ``notifications/resources/updated`` to the stubs subscribed to its URI, so
# subscriptions survive pooling and there is nothing to warn about.
#
# ``*.listChanged`` is deliberately NOT here: those notifications are global
# broadcasts (``backend._GLOBAL_BROADCAST_NOTIFICATIONS``) and are safe to
# fan out to every attached stub.
_SHARED_BACKEND_DEGRADATIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("logging",), "logging_level"),
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


def rotating_secret_env(
    env_names: tuple[str, ...], identity_keys: Collection[str] = ()
) -> tuple[str, ...]:
    """Declared env names whose value is excluded from the pool key.

    ``identity_keys`` is ``mcp_gateway.pool_identity_env`` as resolved by
    :func:`rewriter.pool_identity_env_keys`. A named variable IS in the pool key,
    so it is not one of these — reporting it would tell the operator sharing is
    unsafe for the exact key they made safe, and would withhold a share
    recommendation the rewriter now grants.
    """
    named = frozenset(identity_keys)
    return tuple(
        n
        for n in env_names
        if n not in named and n.upper().startswith(ROTATING_SECRET_ENV_PREFIXES)
    )


def _shared_backend_degradations(capabilities: dict) -> list[Reason]:
    """Capabilities whose behaviour degrades on a shared backend.

    Pure information: see the table's comment for why each entry is a gap in
    our own broker rather than a property of the server, which is what makes
    gating on it charging the operator for work we have not done.

    Detection is by PRESENCE of the capability key. In MCP an empty object is
    the standard way to advertise a capability that takes no sub-options, so
    ``{"logging": {}}`` means ``logging/setLevel`` is supported; testing
    truthiness there would read a server that advertises logging as one that
    does not. A flag-leaf entry — one whose leaf can be ``false``, the server
    explicitly declining the capability — needs a truthiness test instead, so
    adding one to the table means adding a per-entry detection mode alongside
    it; the table deliberately carries no such machinery while no entry needs
    it.
    """
    out: list[Reason] = []
    for path, code in _SHARED_BACKEND_DEGRADATIONS:
        node: object = capabilities
        missing = False
        for key in path:
            if not isinstance(node, dict) or key not in node:
                missing = True
                break
            node = node[key]
        if missing:
            continue
        out.append(Reason("degrades_when_shared", code))
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


def assess(
    evidence: ShareEvidence, identity_keys: Collection[str] = ()
) -> ShareVerdict:
    """Turn evidence into a verdict. Pure; no IO, no config, no clock.

    ``identity_keys`` is passed IN rather than read here precisely to keep that
    contract: it is config, so the caller resolves it (see
    :func:`rewriter.pool_identity_env_keys`).
    """

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

    # 2. The ONE permanent exclusion, and it is not a safety verdict: an HTTP/SSE
    #    server has no stdio pipe to put a stub in front of, so the question does
    #    not apply rather than the answer being no.
    if not evidence.is_stdio:
        return verdict(Strength.DISQUALIFIED, [Reason("not_stdio")])

    # 3. Session-bound by construction is a DISQUALIFIER, and the reason is not
    #    the degradation it looks like.
    #
    #    Such a server resolves its caller from its own process, and gatewayd
    #    forwards no session-identifying env to a shared backend, so on a pooled
    #    backend it reads EMPTY. The tempting conclusion is that empty is benign --
    #    features that need a session quietly stop working. That is wrong, because
    #    what empty MEANS is decided by the consumer, and in the one that matters
    #    it is privileged: ``mcp_cron._check_cron_job_ownership`` does
    #
    #        if not session_key:
    #            return None  # No session context (single-user local mode) -- allow
    #
    #    an empty key FAILS OPEN and the ownership check is skipped entirely. A
    #    pooled cron would therefore let one session list, pause or remove another
    #    session's jobs. That is a cross-session authorization failure, not a lost
    #    feature.
    #
    #    It is also the one case this layer's "share by default and retreat when
    #    something is observed" posture cannot cover: both hazard codes are
    #    routing-shaped, so a server serving the wrong session's data without ever
    #    emitting an unroutable frame produces no ledger entry. There is no retreat
    #    to fall back on, so the gate stays until the servers it names consume the
    #    injected caller block (#4622) -- at which point the input has no producer
    #    and this branch is deleted rather than relaxed.
    #
    #    Checked BEFORE the probe gate: it is a config fact, true whether or not
    #    the server ever started.
    if evidence.session_bound_by_construction:
        return verdict(
            Strength.DISQUALIFIED, [Reason("session_bound_by_construction")]
        )

    # 4. Config-derived notes. Computed BEFORE the probe gate for the same reason.
    #
    #    ``rotating_secret_env`` no longer DISQUALIFIES, because it is not a leak: a
    #    secret-prefixed key is never forwarded into a SHARED backend
    #    (``gatewayd._declared_non_secret_env`` drops it; ``ENV_SCRUB_PREFIXES``
    #    explains why the keys are excluded from the pool hash so rotation cannot
    #    shatter the pool, which makes the hash non-injective over them and no
    #    single value correct). The pooled backend receives NOBODY's secret rather
    #    than the wrong session's.
    #
    #    It does still withhold ``recommend_share``, and for a reason that is not
    #    about risk at all: the rewriter ALREADY refuses to pool such an entry.
    #    ``_withheld_env_count`` counts the keys a shared backend would not receive
    #    -- with forwarding on, exactly this set -- and a non-zero count leaves the
    #    entry unwrapped, which ``_stub_eligibility`` reports as
    #    ``pooling_blocked_by_env``. Its stated ground is that any withheld key can
    #    be the one the server dies without (#3495 cause B). Recommending a share
    #    the rewriter will decline would have the page promise work the broker never
    #    does -- two of our own components disagreeing, which is worse than either
    #    answer. So the verdict follows the guard that actually runs, and when that
    #    guard changes (routing by credential identity, with ``credwatch`` already
    #    handling rotation by content digest) this withholding goes with it.
    #
    #    ``mcp_gateway.pool_identity_env`` is the first instalment of exactly that
    #    change: a named key IS routed by its value, so ``_withheld_env_count`` no
    #    longer counts it and the rewriter does pool the entry. The withholding
    #    follows, via ``identity_keys`` — otherwise the page would decline a share
    #    the broker performs, the same disagreement in the other direction.
    notes: list[Reason] = []
    withhold_share = False
    for name in rotating_secret_env(evidence.declared_env_names, identity_keys):
        notes.append(Reason("rotating_secret_env", name))
        withhold_share = True

    # 4. Nothing observed. Distinguish "never handshook" from "declared nothing".
    if not evidence.probe_ok or evidence.capabilities is None:
        return verdict(Strength.UNKNOWN, [Reason("not_probed")] + notes)

    # 4. Notes that travel with the verdict instead of replacing it.
    #
    #    Both of these used to return DISQUALIFIED, and both were inferences
    #    dressed as findings. They are collected here and appended to whichever
    #    tier the evidence actually supports.
    #
    #    They are NOT equally load-bearing, and the difference is what this layer
    #    is for. It exists to turn pooling ON for an operator who never got round
    #    to it, so "no" is its failure mode rather than its caution.
    #
    #    * ``degrades_when_shared`` can withhold an automatic share, but only for
    #      the entry that names a feature which BREAKS. That is a deterministic
    #      consequence of our own broker, not a guess about the server.
    #    * ``handshake_not_reproducible`` withholds NOTHING. It is the two-identity
    #      comparison finding a difference, and it cannot say WHY: an answer
    #      computed from ``clientInfo`` (the real hazard) and an answer that varies
    #      for the server's own reasons -- startup feature detection, a
    #      reachability probe -- are indistinguishable from two samples that both
    #      vary the identity. The second kind gives every co-tenant of one process
    #      the SAME answer, which is what an unpooled process does too. It is also
    #      re-derived every pass now, so gating on it would make a server's
    #      eligibility flap with the last sample -- a gate nobody can predict is
    #      worse than none. Pure information.
    #
    #    Both still recommend the STUB, which keeps the backend 1:1 with the
    #    session -- the same topology as no gateway at all -- so nothing here is a
    #    reason to withhold that.
    notes += _shared_backend_degradations(evidence.capabilities)
    if evidence.preflight_ran and evidence.preflight_caller_sensitive:
        notes.append(Reason("handshake_not_reproducible"))

    # 5. Positive declaration: the server was written for a pooled backend.
    if _declares_caller_identity(evidence.capabilities):
        reasons = [Reason("declares_caller_identity")]
        if _all_tools_read_only(evidence.tool_annotations):
            reasons.append(Reason("all_tools_read_only"))
        # Sharing still needs the provocation to have actually happened. A
        # declaration plus an unrun pre-flight is a promise nobody has tested.
        #
        # ``withhold_share`` is agreement with the rewriter's own pooling gate, not
        # a second opinion about the server: see its note above. A divergence and a
        # broker-side degradation do NOT reach this, by design -- both describe work
        # we have not done, and charging the operator for that is the failure mode
        # of a layer whose job is to say yes.
        share = evidence.preflight_ran is True and not withhold_share
        # ``preflight_passed`` is a claim that the provocation found nothing, so it
        # must not sit beside ``handshake_not_reproducible`` on the same row --
        # that reads as "answered identically" and "did not answer identically"
        # about one server. A divergence leaves the note to speak for itself.
        diverged = bool(evidence.preflight_ran) and evidence.preflight_caller_sensitive
        if evidence.preflight_ran and not diverged:
            reasons.append(Reason("preflight_passed"))
        elif not evidence.preflight_ran:
            reasons.append(Reason("preflight_not_run"))
        return verdict(Strength.DECLARED, reasons + notes, stub=True, share=share)

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
    #
    #    A pass that RAN and saw the handshake differ has not earned MEASURED
    #    either. MEASURED's whole content is that something was ruled out, and a
    #    divergence rules nothing out -- so it falls to NO_OBJECTION carrying the
    #    note, which is the honest reading: no durable objection exists, and one
    #    sample looked odd.
    measured = bool(evidence.preflight_ran) and not evidence.preflight_caller_sensitive
    reasons = [Reason("preflight_passed" if measured else "no_objection_found")]
    if _all_tools_read_only(evidence.tool_annotations):
        reasons.append(Reason("all_tools_read_only"))
    elif not evidence.tool_annotations:
        reasons.append(Reason("no_tool_annotations", evidence.protocol_version))
    if not evidence.has_tools:
        reasons.append(Reason("no_tools_listed"))
    if measured:
        return verdict(Strength.MEASURED, reasons + notes, stub=True, share=False)
    return verdict(Strength.NO_OBJECTION, reasons + notes, stub=True, share=False)
