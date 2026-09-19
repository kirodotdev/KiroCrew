"""Per-host strategy objects for the shared-process ACP runtime.

``AcpRuntime`` hosts many sessions in one child process. What that child IS --
kiro-cli, the KAS relay, or a backend added later -- is answered here, in one
file per host, rather than by a backend test at each point of difference. There
are twelve such points, and a host that answers eleven of them is a host that
starts and then behaves like a different one.

Start at :mod:`kiro_crew.acp.harness.base`: it names every seam and says what each
one is for. :func:`harness_for` is how the runtime gets the right harness, and it
REFUSES a backend with no harness rather than serving it as kiro-cli.
"""

from __future__ import annotations

from kiro_crew.acp.harness.base import (
    HarnessAdapter,
    NotificationAliases,
    ReclaimPolicy,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    TeardownPolicy,
)
from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.harness.descriptor import HarnessDescriptor
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.harness.operator import DescriptorHarness
from kiro_crew.acp.types import ACP_BACKEND_CODEX, ACP_BACKEND_KAS, ACP_BACKEND_KIRO

__all__ = [
    "CodexHarness",
    "DescriptorHarness",
    "HarnessAdapter",
    "KasHarness",
    "KiroHarness",
    "NotificationAliases",
    "ReclaimPolicy",
    "SessionExtras",
    "SpawnContext",
    "SpawnPlan",
    "TeardownPolicy",
    "harness_for",
    "register_operator_harness",
]

_HARNESSES: dict[str, type[HarnessAdapter]] = {
    ACP_BACKEND_KIRO: KiroHarness,
    ACP_BACKEND_KAS: KasHarness,
    # This table answers "can the shared-process runtime drive this host?", and
    # ``ACP_BACKENDS_ACP_RUNTIME`` answers "does a session take that path?". They
    # agree for every member here, and they are still separate questions: a harness
    # is written and tested before it is routed, so the table has to be reachable
    # while the set does not yet name it. Gating registration on the set would make
    # a harness unreachable to its own tests, and would leave the runtime resolving
    # one that exists on disk but not in the table.
    ACP_BACKEND_CODEX: CodexHarness,
}

# ── The operator (config-authored) register ──
#
# A SEPARATE structure from ``_HARNESSES`` on purpose, and the separation is
# pinned: ``test_the_registry_serves_every_backend_it_claims_to`` asserts
# ``set(_HARNESSES) == {kiro, kas, codex}`` and ``test_the_kiro_lookup_is_total``
# asserts ``_HARNESSES`` is a bare literal in this module built from no
# configuration. Both stay true because a config-authored backend never lands in
# ``_HARNESSES`` -- it lands here, mapping its backend id to the descriptor
# :class:`DescriptorHarness` is built from. ``harness_for`` consults the literal
# FIRST (so kiro's lookup is total, literal and side-effect-free -- H13) and this
# register second.
#
# Kept a private mutable dict for the reason ``backends._registered_known`` and
# the ``_baseline``/``_selectable`` pair are: a second binding is how two views of
# one registry start disagreeing, so it has one home and one writer
# (:func:`register_operator_harness`) plus one test-only reset.
_OPERATOR_REGISTER: dict[str, HarnessDescriptor] = {}


def register_operator_harness(descriptor: HarnessDescriptor) -> None:
    """Make ``descriptor`` resolvable through :func:`harness_for`.

    Called at boot by the bootstrap wiring (W2-P3), once per valid operator
    descriptor, BEFORE the first session resolves a harness. Records the
    descriptor under its own id so ``harness_for`` can build a
    :class:`DescriptorHarness` for it.

    Refuses an id ``_HARNESSES`` already serves (a builtin) and a duplicate
    registered id: a silent overwrite is how one descriptor's harness clobbers
    another's, and a builtin id must resolve to its hand-written class, never a
    descriptor. This mirrors :func:`kiro_crew.agent_sdk.backends.register_known_backend`,
    which refuses the same collisions on the vocabulary side -- the two seams are
    written together at boot, so an id that is known must also be operator-servable
    and vice versa, and matching refusals keep them from drifting.
    """
    backend_id = descriptor.id
    if backend_id in _HARNESSES:
        raise ValueError(
            f"cannot register operator harness {backend_id!r}: it is a builtin the "
            f"shared-process runtime already serves with a hand-written harness"
        )
    if backend_id in _OPERATOR_REGISTER:
        raise ValueError(
            f"cannot register operator harness {backend_id!r}: an operator harness "
            f"is already registered under that id"
        )
    _OPERATOR_REGISTER[backend_id] = descriptor


def _reset_operator_register() -> None:
    """TEST-ONLY: drop every operator harness, restoring the builtin-only state.

    The paired teardown for :func:`register_operator_harness`, mirroring
    ``backends._reset_registered_backends``. Registration is boot-once and never
    undone in a running gateway, so nothing in product code calls this.
    """
    _OPERATOR_REGISTER.clear()


def harness_for(backend: str) -> HarnessAdapter:
    """The harness for ``backend``.

    Consults the builtin literal ``_HARNESSES`` FIRST, then the operator register.
    The order is load-bearing: it keeps kiro's lookup total, literal and
    side-effect-free (H13) -- the default path resolves through the literal and
    never touches the operator register at all -- while making a config-authored
    backend resolvable through the same one function every caller already uses.

    Raises ``ValueError`` for a backend with no harness in EITHER, naming the full
    served set (builtin + operator). Failing here is the point: a backend the
    runtime has no harness for would otherwise silently inherit kiro-cli's spawn
    argv, protocol version and teardown verb, and the first sign of it would be a
    session that starts and then behaves wrongly.
    """
    builtin = _HARNESSES.get(backend)
    if builtin is not None:
        return builtin()
    descriptor = _OPERATOR_REGISTER.get(backend)
    if descriptor is not None:
        return DescriptorHarness(descriptor)
    served = sorted(set(_HARNESSES) | set(_OPERATOR_REGISTER))
    raise ValueError(
        f"no ACP harness for backend {backend!r}; the shared-process runtime serves {served}"
    )
