"""The leaf ``ACP_BACKENDS_*`` sets and the bundled descriptors must agree.

Wave-2 T4 rekeyed every PER-SESSION behavior consumer onto
``binding.descriptor.capabilities`` at its own call site, and T5 deleted the
derived behavior views from ``acp/types.py``. The merge with origin/main then
brought in upstream's parallel refactor: the capability sets are DEFINED in the
leaf ``kiro_crew.acp_backends`` (so a consumer outside the ACP package can ask a
capability question without importing ``kiro_crew.acp``, whose ``__init__``
pulls in the client and runtime) and re-exported from ``acp.types`` for existing
importers — ``test_acp_capability_sets_leaf.py`` pins that placement.

Post-merge, both disciplines hold at once:

* PER-SESSION behavior gates still read the session's bound descriptor
  (``_declares(<CAPABILITY_*>)``), never a set keyed on the legacy
  ``acp_backend`` spelling — an operator harness has no such spelling, so a set
  lookup would silently mis-gate it. The harness-parity added-line gate
  (``scripts/check_harness_parity.py``) keeps new membership checks out of the
  gated modules.
* OUTSIDE-ACP consumers with no session in hand (readiness, prerequisites,
  ``session._bg_runtime_backends``) read the leaf sets, whose membership is the
  bundled ``acp_backend`` vocabulary by construction.

That leaves ONE new way to be wrong: the leaf sets and the descriptors'
``CapabilitySet`` grants are two spellings of the same facts and can drift.
This module is the pin that keeps them agreeing — each leaf set must equal the
same membership rebuilt from ``BUNDLED_DESCRIPTORS``, and both must equal the
shipped literals recorded here (kiro-cli full; KAS steer+runtime+identity;
claude none), so a drift in either spelling fails by name.
"""

from __future__ import annotations

from kiro_crew.acp import types as acp_types
from kiro_crew.acp.harness_descriptor import (
    CAPABILITY_ACP_RUNTIME_POOL,
    CAPABILITY_INTERNAL_SANDBOX,
    CAPABILITY_KIRO_IDENTITY_STORE,
    CAPABILITY_SESSION_SHARING,
    CAPABILITY_STEER,
)
from kiro_crew.acp.harness_registry import BUNDLED_DESCRIPTORS
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.acp_backends import (
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    selectable_backends,
)

#: The vocabulary sets that STAY (the ``acp_backend`` gate, not capability views).
#: ``ACP_BACKENDS_SELECTABLE`` was dropped in wave-2 for the ``selectable_backends()``
#: registry function (checked separately below), so only ``ACP_BACKENDS_KNOWN``
#: remains as a module-level frozenset here.
_VOCABULARY_SETS = ("ACP_BACKENDS_KNOWN",)

#: capability flag -> (leaf set, shipped membership). The literals are the pin's
#: independent third leg: with only two spellings, a bug edited into both at once
#: would still "agree". kiro-cli full; KAS steer+runtime+identity; claude none.
_CAPABILITY_TRIPLES = {
    CAPABILITY_SESSION_SHARING: (
        ACP_BACKENDS_SESSION_SHARING,
        frozenset({ACP_BACKEND_KIRO}),
    ),
    CAPABILITY_STEER: (
        ACP_BACKENDS_STEER,
        frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
    ),
    CAPABILITY_INTERNAL_SANDBOX: (
        ACP_BACKENDS_INTERNAL_SANDBOX,
        frozenset({ACP_BACKEND_KIRO}),
    ),
    CAPABILITY_ACP_RUNTIME_POOL: (
        ACP_BACKENDS_ACP_RUNTIME,
        frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
    ),
    CAPABILITY_KIRO_IDENTITY_STORE: (
        ACP_BACKENDS_KIRO_IDENTITY_STORE,
        frozenset({ACP_BACKEND_KIRO, ACP_BACKEND_KAS}),
    ),
}


def _backends_claiming_locally(capability: str) -> frozenset[str]:
    """The set-of-backends membership rebuilt from the bundled descriptors.

    Keyed by the legacy ``acp_backend`` spelling, so a bundled harness without
    one (Codex), and every operator harness, contributes no member — exactly the
    leaf sets' own membership rule.
    """
    return frozenset(
        acp_types._HARNESS_BACKENDS[d.id]
        for d in BUNDLED_DESCRIPTORS
        if d.id in acp_types._HARNESS_BACKENDS and d.capabilities.has(capability)
    )


def test_leaf_sets_descriptors_and_shipped_literals_all_agree() -> None:
    """Each capability's three spellings are identical.

    A descriptor grant edited without its leaf set (or vice versa) fails here by
    capability name, which is what keeps "claimed in one vocabulary, withheld in
    the other" impossible — the property the derived views used to provide by
    construction.
    """
    for capability, (leaf_set, shipped) in _CAPABILITY_TRIPLES.items():
        rebuilt = _backends_claiming_locally(capability)
        assert (
            leaf_set == shipped
        ), f"{capability}: leaf set {sorted(leaf_set)!r} != shipped {sorted(shipped)!r}"
        assert (
            rebuilt == shipped
        ), f"{capability}: descriptors rebuild {sorted(rebuilt)!r} != shipped {sorted(shipped)!r}"


def test_the_collector_is_gone() -> None:
    """``_backends_claiming`` — which derived the old views — stays deleted.

    Left behind, it is the one call a session-scoped consumer would quietly
    rebuild a view from; per-session gates read the bound descriptor instead.
    """
    assert not hasattr(acp_types, "_backends_claiming")


def test_the_vocabulary_gate_survives() -> None:
    """The ``acp_backend`` vocabulary gate is NOT a view and must remain.

    Config resolution (``_normalize_acp_backend``) and provider construction key
    on it, so the capability-set placement must not take it with it. Post-merge
    the gate is two things: the ``ACP_BACKENDS_KNOWN`` frozenset (the closed
    membership set) and the ``selectable_backends()`` registry function that
    replaced the dropped ``ACP_BACKENDS_SELECTABLE`` snapshot.
    """
    for name in _VOCABULARY_SETS:
        assert isinstance(
            getattr(acp_types, name), frozenset
        ), f"{name} is the acp_backend vocabulary gate and must stay a frozenset"
    # The selectable set is now a registry function, not a frozen constant, but it
    # is still part of the vocabulary gate and must still hand back a frozenset.
    assert isinstance(
        selectable_backends(), frozenset
    ), "selectable_backends() is the acp_backend selectable gate and must return a frozenset"
