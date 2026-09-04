"""What a live session's backend can do — asked instead of which backend it is.

Application code has six branches that depend on a PROPERTY of the harness: which
model-id namespace it uses, whether its advertised list must be read back, which
channel carries an effort change, whether compaction finishes inline, and which
provider seam serves the session. Naming the harness instead of the property has
one failure mode, always in the same direction. A fifth backend arrives, nobody
edits the branch, and it silently takes whichever arm "not claude" happens to
select — an arm it never demonstrated it can serve.
``docs/system-specs/modules/harness-parity.md`` calls this H6; RFC PR 3 is where it
stops being possible outside the boundary.

So this module carries one frozen record per session, with one field per question
a consumer OUTSIDE ``kiro_crew.agent_sdk`` actually asks, and consumers read the
field. The identity of the backend is still in :attr:`SessionCapabilities.backend`
because a log line and an error message need to name it — but nothing branches on
it, and ``test_agent_sdk_capabilities`` pins that.

Every field is a TRANSLATION of a table that already existed in
:mod:`kiro_crew.agent_sdk.backends`, not a new judgement. That is deliberate and
it is the whole reason this can land as a refactor: each field reproduces, for
every backend id including ``codex`` and including an unknown id, exactly the
answer the branch it replaces already computed. The disposition table in
``backends``'s docstring records which set each field translates.

Fail-closed on an unknown id
----------------------------
:func:`capabilities_for` never raises and never guesses. An id this build does not
know resolves to :data:`UNKNOWN_BACKEND_CAPABILITIES` — every boolean False and
both string axes on their default — which is the same answer the old
``== ACP_BACKEND_CLAUDE`` comparisons gave an unrecognized value. False here means
"has not demonstrated the capability", so the arm a stranger takes is the
conservative one rather than the one the previous member happened to leave behind.

Why a record and not a bag of module functions
----------------------------------------------
:func:`capabilities_of` resolves the record from a PROVIDER, and the isinstance
check it performs is load-bearing rather than defensive noise. The predicates this
replaces were ``isinstance(provider, AcpProvider) and provider.is_claude_backend``,
so a shape that was not a provider answered False. A bare
``getattr(provider, "capabilities", None)`` would not preserve that:
``MagicMock(spec=AcpProvider).capabilities`` is a truthy Mock whose every
attribute is also truthy, so a spec'd test double would start reading as a member
of every capability at once. Requiring a real :class:`SessionCapabilities`
instance keeps an unknown shape on the fail-closed default, which is what the
isinstance gate bought.
"""

from __future__ import annotations

from dataclasses import dataclass

from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.agent_sdk.backends import (
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_INLINE_COMPACTION,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP, PROVIDER_CLAUDE_CODE

#: The model-registry namespace kiro's own ids live in, and the one every backend
#: that has not declared another falls back to. Named so a consumer can ask
#: "is this the native namespace?" without spelling the literal.
MODEL_NAMESPACE_ACP = "acp"


@dataclass(frozen=True)
class SessionCapabilities:
    """What the backend serving one session can do.

    Frozen, and cheap enough to build per read: every field is a set membership or
    a dict lookup over four ids, so there is no cache to invalidate when an edition
    registers a backend mid-process.
    """

    #: The ``agent.acp_backend`` id this record describes. For messages and logs.
    #: Nothing above the boundary may branch on it — that is the whole point of
    #: the fields below.
    backend: str

    #: Which provider SEAM serves the session: :data:`PROVIDER_ACP` or
    #: :data:`PROVIDER_CLAUDE_CODE`.
    #:
    #: The ``agent.provider`` axis, not the ``acp_backend`` axis — see
    #: :mod:`kiro_crew.agent_sdk.provider_identity` for why conflating them draws
    #: the wrong conclusion from either. Consumers use it to LABEL a session (a
    #: billing row's provider) and to route session-file cleanup to the right
    #: home tree (``~/.claude`` vs ``~/.kiro``).
    #:
    #: Known residue, inherited unchanged: KAS reads as :data:`PROVIDER_ACP`. Only
    #: the ACP layer's own ``PROVIDER_LABEL_*`` constants distinguish it, and
    #: promoting that distinction here would change what every KAS turn records.
    #: A widening belongs in its own change with its own reason.
    provider_seam: str

    #: The model-registry namespace this backend's model ids live in.
    #:
    #: A registry index key, NOT a provider-identity check: a context window is a
    #: property of the model, so the same model reached two ways shares one
    #: namespace. :data:`MODEL_NAMESPACE_ACP` means the backend takes kiro's own
    #: ids and expresses "let the provider choose" as the real id ``auto``; any
    #: other value means ids are translated into that provider's namespace and
    #: there is no id meaning "choose for me", so returning to default needs a
    #: session reset rather than a switch.
    model_id_namespace: str

    #: Whether the WIRE model id must be resolved against the list this backend
    #: advertised on ``session/new``, rather than trusting a stored id verbatim.
    #:
    #: True where the spelling the backend SERVES differs from the one Crew stored
    #: (claude-agent-acp advertises versioned ``…[1m]`` ids). Two consumers ride
    #: it: the models picker reads the advertised list from such a session, and the
    #: pinned-model availability verdict SKIPS such a session, because comparing a
    #: stored id against a differently-spelled advertised list manufactures false
    #: "model unavailable" withholds.
    resolves_model_from_advertised_list: bool

    #: Whether a reasoning-effort change goes through
    #: ``session/set_config_option("effort", …)`` rather than the kiro-native
    #: ``/effort`` slash command.
    #:
    #: A change sent down a channel the adapter does not implement is answered
    #: with method-not-found, and the session keeps serving turns at the level the
    #: operator thought they had just left.
    effort_via_config_option: bool

    #: Whether a manual ``/compact`` finishes inside the ``session/prompt`` turn.
    #:
    #: True means the turn's terminal frame is the done signal and the caller
    #: acknowledges immediately. False means the result arrives separately and the
    #: caller must await it; awaiting a member instead strands that wait for its
    #: full timeout.
    compacts_inline: bool


def capabilities_for(backend: str) -> SessionCapabilities:
    """The capabilities of *backend*, fail-closed for an id this build cannot name.

    Pure and total: no raise, no I/O, no dependence on whether a session exists.
    That is what lets ``config.loader`` call it from inside
    ``KiroCrewConfig.load()``, where reaching the platform context would re-enter
    the very load that called it.
    """
    return SessionCapabilities(
        backend=backend,
        provider_seam=(PROVIDER_CLAUDE_CODE if is_claude_backend_name(backend) else PROVIDER_ACP),
        model_id_namespace=model_registry_namespace(backend),
        resolves_model_from_advertised_list=backend in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
        effort_via_config_option=backend in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
        compacts_inline=backend in ACP_BACKENDS_INLINE_COMPACTION,
    )


#: The answer for a backend id this build does not know, and for a shape that is
#: not a provider at all.
#:
#: Not a sentinel a caller has to test for: it is a real record whose every
#: capability is withheld, so a consumer that never checks still takes the
#: conservative arm. Spelled out rather than derived from a fake id so the
#: fail-closed answer is readable here; ``test_agent_sdk_capabilities`` pins it
#: equal to what :func:`capabilities_for` returns for an unknown id, so the two
#: cannot drift.
#:
#: ``backend`` is the empty string because there is no id to report. That is also
#: the kiro backend's own spelling, so this record is NOT how you tell "unknown"
#: from "kiro" — nothing above the boundary may branch on that field anyway.
UNKNOWN_BACKEND_CAPABILITIES = SessionCapabilities(
    backend="",
    provider_seam=PROVIDER_ACP,
    model_id_namespace=MODEL_NAMESPACE_ACP,
    resolves_model_from_advertised_list=False,
    effort_via_config_option=False,
    compacts_inline=False,
)


def capabilities_of(provider: object) -> SessionCapabilities:
    """The capabilities of the backend *provider* is talking to.

    Accepts any shape and answers :data:`UNKNOWN_BACKEND_CAPABILITIES` for one
    that does not carry a real :class:`SessionCapabilities`. That is the calling
    convention the six predicates this replaces already had — they were
    ``isinstance``-gated and answered False off a foreign shape — and it is what
    keeps a wrapper, an unstarted provider or a test double from reading as a
    member of every capability at once.
    """
    caps = getattr(provider, "capabilities", None)
    return caps if isinstance(caps, SessionCapabilities) else UNKNOWN_BACKEND_CAPABILITIES


__all__ = [
    "MODEL_NAMESPACE_ACP",
    "SessionCapabilities",
    "UNKNOWN_BACKEND_CAPABILITIES",
    "capabilities_for",
    "capabilities_of",
]
