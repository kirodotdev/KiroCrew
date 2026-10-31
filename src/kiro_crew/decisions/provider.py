"""The decision-provider contract, and the closed registry the gate picks from.

A provider answers typed questions about a state: ``ask(state, questions)``
returns every answer or raises, never a partial mapping, never a retry. The gate
(``decisions.gate``) owns everything around that call -- consent, scopes, the
fleet ceiling, the scrub, the budget, the domain re-check and the log -- so a
provider is only the transport and the wire mapping. That split is what lets one
gate serve several providers without any of them restating a refusal.

Each provider is described by a :class:`ProviderSpec`: its ``kind`` (hosted,
local, session LLM), which LANE it answers on (the value the log, the dashboard
and ``decisions.nudge_wake.provider`` carry), where its request goes, what
authorizes it, which capability row governs it, which question types it
answers, and the window its budget is clamped into. The spec is DATA and grants
nothing: authorization stays in the gate, which today reads only the budget
window from it.

The registry is a closed table, not a plug-in hook. Adding a provider is an edit
to :data:`_REGISTRY` that a reviewer reads beside the gate, because each entry
names a destination conversation text can reach.

Imports nothing heavy: every factory imports its implementation at call time, so
neither ``httpx`` nor the session layer lands on a hot caller's import graph,
and a test that patches an implementation's class attribute is honoured.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

from kiro_crew.decisions.types import Answers, Question

#: The lane a keystone-authorized endpoint answers on. The VALUE is ``"jev"``
#: because that is the word already persisted in ``decisions.nudge_wake.provider``,
#: in judge rows on the dashboard and in the decision log; it now names the
#: configured System One endpoint, hosted or local, not one vendor.
LANE_ENDPOINT = "jev"

#: The lane answered by one tool-less model call on the session's own provider.
LANE_SESSION_LLM = "llm"

#: Every lane, in the order the gate documents them.
LANES = (LANE_ENDPOINT, LANE_SESSION_LLM)

#: What a provider is. Decides which capability row and which credential rule
#: apply to it.
KIND_HOSTED = "hosted"
KIND_LOCAL = "local"
KIND_SESSION_LLM = "session_llm"

#: Where a provider's request goes. A third party is off the machine; loopback is
#: an attested local server; the session provider is the model provider the
#: owner's sessions already send every turn to.
EGRESS_THIRD_PARTY = "third-party"
EGRESS_LOOPBACK = "loopback"
EGRESS_SESSION_PROVIDER = "session-provider"

#: What authorizes a provider to be asked. ``keystone`` is
#: ``decisions_consent.json`` for the configured endpoint plus the point's scopes;
#: ``session-provider`` is the owner's own lane choice, with the fleet ceiling
#: still binding (``decisions.md`` section 14).
AUTHORITY_KEYSTONE = "keystone"
AUTHORITY_SESSION_PROVIDER = "session-provider"

#: The governance rows a provider answers to (``decisions.capability``). The local
#: row only narrows: a provider governed by it is also held to the hosted row.
ROW_DECISIONS = "capabilities.decisions"
ROW_DECISIONS_LOCAL = "capabilities.decisions_local"

#: The text question types this build declares (``decisions.types``), as wire words.
QUESTION_KINDS = frozenset({"choice", "noul", "score"})

#: The image-only question type and the image capability. A spec declares them in
#: ``supports`` only when its client can carry an image to a provider that reads
#: one; the gate sends neither to a spec that does not (``gate.decide``).
KIND_BOX = "box"
CAPABILITY_IMAGE = "image"

#: Registry ids. Stable identifiers for code and tests, never persisted.
PROVIDER_HOSTED = "systemone-hosted"
PROVIDER_LOCAL = "systemone-local"
PROVIDER_SESSION_LLM = "session-llm"

#: The session-LLM lane's budget window in seconds. ``provider.timeout_ms`` is
#: sized for a ~100 ms System One call, which a text model cannot answer inside,
#: so the configured value is clamped UP into this window rather than replaced.
SESSION_LLM_TIMEOUT_WINDOW = (8.0, 60.0)


@runtime_checkable
class DecisionProvider(Protocol):
    """What the gate calls. Raise on any failure; return every answer or none.

    An implementation MAY also expose ``last_response_meta`` -- a small mapping of
    provider-stamped fields, set only after a fully valid response -- which the
    gate merges onto the success row. It is optional, so it is not part of the
    protocol's required surface.
    """

    async def ask(self, state: dict | str, questions: list[Question]) -> Answers: ...


# A provider whose spec declares :data:`CAPABILITY_IMAGE` also accepts
# ``assets={asset_id: ImageAsset}`` as a keyword on ``ask``; the gate passes it
# only to such a provider, so every other one keeps the two-argument call.


#: Builds one provider for one decision from the ``decisions.provider`` section
#: and the model id the gate already scrubbed.
ProviderFactory = Callable[[Any, str], DecisionProvider]


@dataclass(frozen=True)
class Capabilities:
    """What the provider that would answer can do, for a caller that adapts to it.

    A caller reads these instead of a provider's name: one request at a time means
    no pipelining. (Whether images may be sent is the gate's own check,
    ``gate.provider_supports``.) Neither grants anything; consent and scopes stay
    in the gate.
    """

    max_in_flight: int
    latency_ms: int


@dataclass(frozen=True)
class ProviderSpec:
    """One registered provider: what it is, where it sends, and how it is built."""

    id: str
    kind: str
    lane: str
    egress: str
    authority: str
    #: The capability row that governs this provider (see :data:`ROW_DECISIONS`).
    governance_row: str
    #: The question types it answers, from :data:`QUESTION_KINDS`, plus
    #: :data:`KIND_BOX` and :data:`CAPABILITY_IMAGE` where it can take an image.
    supports: frozenset[str]
    #: ``(min, max)`` seconds the budget is clamped into, or ``None`` to use
    #: ``provider.timeout_ms`` as configured.
    timeout_window: tuple[float, float] | None
    factory: ProviderFactory
    #: How many requests it takes at once without queueing them, as declared by its
    #: client: a caller that pipelines (the browser's ``reflex`` realtime mode)
    #: keeps at most this many in flight, and one that needs more falls back to a
    #: mode that asks one at a time.
    max_in_flight: int = 1
    #: Typical answer time in milliseconds, as declared. A caller reads it only to
    #: pick a pace before it has measured its own; it never bounds a request.
    latency_ms: int = 1000

    def capabilities(self) -> Capabilities:
        """What a caller may plan around, read off this spec."""
        return Capabilities(
            max_in_flight=self.max_in_flight,
            latency_ms=self.latency_ms,
        )

    @property
    def local(self) -> bool:
        """Whether ``capabilities.decisions_local`` also narrows this provider."""
        return self.kind == KIND_LOCAL

    def build(self, provider_config: Any, model: str) -> DecisionProvider:
        """A fresh provider for one decision. May raise; the gate catches it."""
        return self.factory(provider_config, model)


def _build_system_one(provider_config: Any, _model: str) -> DecisionProvider:
    """The System One HTTP client, resolved at call time.

    It reads ``provider.model`` itself with the same fallback the gate scrubbed,
    so the model argument is not needed here.
    """
    module = importlib.import_module("kiro_crew.decisions.impl_systemone")
    return module.SystemOneProvider(provider_config)  # type: ignore[no-any-return]


def _build_session_llm(_provider_config: Any, model: str) -> DecisionProvider:
    """One tool-less model call; the inherit sentinel asks for no model at all."""
    module = importlib.import_module("kiro_crew.decisions.impl_llm")
    inherit = module.JUDGE_MODEL_DEFAULT
    return module.LlmProvider(model="" if model == inherit else model)  # type: ignore[no-any-return]


_REGISTRY: dict[str, ProviderSpec] = {
    spec.id: spec
    for spec in (
        ProviderSpec(
            id=PROVIDER_HOSTED,
            kind=KIND_HOSTED,
            lane=LANE_ENDPOINT,
            egress=EGRESS_THIRD_PARTY,
            authority=AUTHORITY_KEYSTONE,
            governance_row=ROW_DECISIONS,
            # The hosted service reads images and answers Box; the local presets
            # in the catalog are text models, so their spec declares neither.
            supports=QUESTION_KINDS | {KIND_BOX, CAPABILITY_IMAGE},
            timeout_window=None,
            factory=_build_system_one,
            # One HTTP/2 connection carries many streams; measured p50 ~150 ms.
            max_in_flight=8,
            latency_ms=150,
        ),
        ProviderSpec(
            id=PROVIDER_LOCAL,
            kind=KIND_LOCAL,
            lane=LANE_ENDPOINT,
            egress=EGRESS_LOOPBACK,
            authority=AUTHORITY_KEYSTONE,
            governance_row=ROW_DECISIONS_LOCAL,
            supports=QUESTION_KINDS,
            timeout_window=None,
            factory=_build_system_one,
            # A CPU model server answers one request at a time.
            max_in_flight=1,
            latency_ms=400,
        ),
        ProviderSpec(
            id=PROVIDER_SESSION_LLM,
            kind=KIND_SESSION_LLM,
            lane=LANE_SESSION_LLM,
            egress=EGRESS_SESSION_PROVIDER,
            authority=AUTHORITY_SESSION_PROVIDER,
            governance_row=ROW_DECISIONS,
            supports=QUESTION_KINDS,
            timeout_window=SESSION_LLM_TIMEOUT_WINDOW,
            factory=_build_session_llm,
            max_in_flight=1,
            latency_ms=8000,
        ),
    )
}


def providers() -> tuple[ProviderSpec, ...]:
    """Every registered provider, in registration order."""
    return tuple(_REGISTRY.values())


def get(provider_id: str) -> ProviderSpec:
    """The spec registered under *provider_id*. ``KeyError`` for an unknown id."""
    return _REGISTRY[provider_id]


def select(lane: str, *, endpoint: object = "", model: object = "") -> ProviderSpec:
    """The provider that answers *lane* for the configured *endpoint* / *model*.

    The session-LLM lane has one provider. The endpoint lane has two that share
    one client and differ only in what governs them: an address with the shape
    the provider route builds for a catalog preset selects the local spec,
    anything else -- including a hand-written loopback address, which may be a
    tunnel to a hosted provider -- the hosted one. The shape alone is enough HERE
    because the gate has already refused a preset-shaped address whose server the
    runtime does not attest (``gate._consented_for``), so by the time a provider
    is selected the shape and the attestation agree. Any lane other than the
    session LLM reads as the endpoint lane, which is the gate's long-standing
    default.
    """
    if lane == LANE_SESSION_LLM:
        return _REGISTRY[PROVIDER_SESSION_LLM]
    # Read from the catalog directly, so selecting a provider does not import the
    # governance graph ``capability`` pulls in. A catalog preset only: ``none``
    # and ``custom`` are not local presets.
    from kiro_crew.decisions import local_models

    if local_models.get(local_models.active_id(endpoint, model)) is not None:
        return _REGISTRY[PROVIDER_LOCAL]
    return _REGISTRY[PROVIDER_HOSTED]


def timeout_window(lane: str) -> tuple[float, float] | None:
    """The budget window for *lane*, or ``None`` when the configured value stands.

    Keyed by lane rather than by provider id because the budget is needed before
    the endpoint is read, and both endpoint providers share a window.
    """
    for spec in _REGISTRY.values():
        if spec.lane == lane:
            return spec.timeout_window
    return None
