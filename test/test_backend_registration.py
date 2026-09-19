"""The backend REGISTRATION seam: a derived ``ACP_BACKENDS_KNOWN`` + ``register_known_backend``.

A frozen ``ACP_BACKENDS_KNOWN`` literal blocks the one thing a
config-authored (out-of-tree) harness needs: teaching this build to *spell* an id the
core has never heard of. It is a live view of ``base ∪ registered``, and
:func:`register_known_backend` is the seam that adds to the registered half and records
the facts every "for every known backend" parity gate demands.

Two properties are pinned here:

* the derived view is a drop-in for the frozen set it replaced — every operation a
  call site or a parity test performs on ``ACP_BACKENDS_KNOWN`` (``in``, iteration,
  ``sorted``/``set``/``len``, subset checks in BOTH directions, set algebra) still
  answers correctly, and at rest the view equals the frozen builtin base; and
* registration makes an id known, routed, labelled, policy-nameable and
  own-namespaced WITHOUT defining any ``ACP_BACKEND*`` constant outside the
  vocabulary module — and does not, by itself, make it selectable.

Every test that registers restores the registry in a ``finally``/fixture, mirroring
the ``_baseline``/``_selectable`` snapshot pattern the selectable-registry tests use,
so one test's registration cannot leak into another (the module state is process
global). Reached through ``agent_sdk.backends``, the module that DEFINES the seam.
"""

from __future__ import annotations

import pytest

from kiro_crew.agent_sdk import backends as b


@pytest.fixture
def clean_registry():
    """Snapshot/restore every registry surface a registration writes.

    ``_reset_registered_backends`` drops registered ids and their routing/label/
    namespace rows; the selectable pair is restored separately because a test may
    also call ``register_selectable_backend`` on a registered id.
    """
    baseline = set(b._baseline)
    selectable = set(b._selectable)
    yield
    b._reset_registered_backends()
    b._baseline.clear()
    b._baseline.update(baseline)
    b._selectable.clear()
    b._selectable.update(selectable)


# ---------------------------------------------------------------------------
# The derived view is a drop-in for the frozen set
# ---------------------------------------------------------------------------

_BUILTINS = ["", "claude", "codex", "deepseek", "goose", "kas", "opencode", "pi"]


def test_at_rest_the_view_is_exactly_the_builtin_base() -> None:
    """Nothing registered => the known set is the eight ids the frozen literal held.

    This is what keeps every existing pin that spells the closed set (e.g.
    ``test_agent_sdk_capabilities.test_known_membership_is_unchanged_by_the_move``)
    green: the derivation adds nothing until someone registers.
    """
    assert sorted(b.ACP_BACKENDS_KNOWN) == _BUILTINS
    assert b.known_backends() == frozenset(_BUILTINS)
    assert b.known_backends() == b._ACP_BACKENDS_BUILTIN


def test_membership_and_len_and_iteration() -> None:
    assert "" in b.ACP_BACKENDS_KNOWN
    assert "codex" in b.ACP_BACKENDS_KNOWN
    assert "byo-harness" not in b.ACP_BACKENDS_KNOWN
    assert len(b.ACP_BACKENDS_KNOWN) == 8
    assert set(b.ACP_BACKENDS_KNOWN) == set(_BUILTINS)
    assert sorted(iter(b.ACP_BACKENDS_KNOWN)) == _BUILTINS


def test_subset_checks_resolve_in_both_directions() -> None:
    """The reflected-operator behaviour the parity gates depend on.

    ``frozenset <= view`` has ``frozenset.__le__`` return ``NotImplemented``, so
    Python falls to ``view.__ge__`` (from ``abc.Set``); ``set >= view`` falls to
    ``view.__le__``. Both must work, because ``test_harness_parity`` does the first
    (``capability_set <= ACP_BACKENDS_KNOWN``) and ``test_acp_deepseek_backend`` does
    the second (``set(ACP_BACKEND_ROUTING) >= ACP_BACKENDS_KNOWN``).
    """
    assert b.ACP_BACKENDS_STEER <= b.ACP_BACKENDS_KNOWN
    assert b.ACP_BACKENDS_INTERNAL_SANDBOX <= b.ACP_BACKENDS_KNOWN
    assert set(b.ACP_BACKEND_ROUTING) >= b.ACP_BACKENDS_KNOWN
    assert set(b.POLICY_ID_BY_BACKEND) == set(b.ACP_BACKENDS_KNOWN)


def test_set_algebra_returns_plain_frozensets() -> None:
    """``|`` / ``-`` / ``&`` yield a frozenset, never another view.

    Call sites do ``frozenset(ACP_BACKENDS_KNOWN | {"fakebackend"})`` and
    ``sorted(ACP_BACKENDS_KNOWN - SOME_SET)``; the results must be ordinary sets.
    """
    union = b.ACP_BACKENDS_KNOWN | {"z"}
    assert isinstance(union, frozenset)
    assert "z" in union and "codex" in union

    diff = b.ACP_BACKENDS_KNOWN - {"codex"}
    assert isinstance(diff, frozenset)
    assert "codex" not in diff and "kas" in diff

    inter = b.ACP_BACKENDS_KNOWN & {"codex", "nope"}
    assert isinstance(inter, frozenset)
    assert inter == frozenset({"codex"})

    # The exact shape test_provider_mirrors uses.
    assert type(frozenset(b.ACP_BACKENDS_KNOWN | {"fakebackend"})) is frozenset


def test_the_view_is_unhashable() -> None:
    """Its contents change across a registration, so it must never key a dict/set.

    ``abc.Set`` grants no ``__hash__``, which is the property we want; asserted so a
    future edit that adds one (making a stale snapshot storable) goes red.
    """
    with pytest.raises(TypeError):
        {b.ACP_BACKENDS_KNOWN}  # noqa: B015 - the hashing attempt is the assertion


# ---------------------------------------------------------------------------
# register_known_backend: the happy paths
# ---------------------------------------------------------------------------


def test_registering_makes_an_unknown_id_known(clean_registry) -> None:
    """The whole point: an id the core never spelled becomes known live.

    Visible through the view, through ``known_backends()``, and to
    ``AcpProvider.__init__`` — which stops rejecting it (covered below).
    """
    assert "acme" not in b.ACP_BACKENDS_KNOWN
    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    assert "acme" in b.ACP_BACKENDS_KNOWN
    assert "acme" in b.known_backends()
    assert "acme" in sorted(b.ACP_BACKENDS_KNOWN)
    assert len(b.ACP_BACKENDS_KNOWN) == 9


def test_registration_records_routing_policy_label_and_namespace(clean_registry) -> None:
    """Every "for every known backend" gate is satisfiable for the registered id.

    routing -> ``ACP_BACKEND_ROUTING`` (so ``routing_for`` answers a real mechanism,
    and ``set(ACP_BACKEND_ROUTING) >= ACP_BACKENDS_KNOWN`` stays true); policy id ->
    ``POLICY_ID_BY_BACKEND`` (so it is nameable in a rule, and
    ``set(POLICY_ID_BY_BACKEND) == set(ACP_BACKENDS_KNOWN)`` stays true); label ->
    resolvable (so "every known backend has a label" holds); namespace -> own bucket.
    """
    b.register_known_backend(
        "acme", label="Acme Coder", routing=b.Routing.AGENT_SPEC, model_namespace="acme"
    )
    assert b.routing_for("acme") is b.Routing.AGENT_SPEC
    assert b.POLICY_ID_BY_BACKEND["acme"] == "acme"  # self-named wire spelling
    assert b.provider_label_for("acme") == "Acme Coder"
    assert b.model_registry_namespace("acme") == "acme"
    # The two "== ACP_BACKENDS_KNOWN" identities existing tests rely on still hold.
    assert set(b.ACP_BACKEND_ROUTING) >= b.ACP_BACKENDS_KNOWN
    assert set(b.POLICY_ID_BY_BACKEND) == set(b.ACP_BACKENDS_KNOWN)


def test_model_namespace_defaults_to_the_backend_id(clean_registry) -> None:
    """Omitting ``model_namespace`` gives the id its own bucket, never kiro's ``acp``."""
    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    assert b.model_registry_namespace("acme") == "acme"
    assert b.model_registry_namespace("acme") != b.model_registry_namespace(b.ACP_BACKEND_KIRO)


def test_session_config_registration_records_permission_config(clean_registry) -> None:
    """A SESSION_CONFIG id records its enforced ``(option, value)`` for the gate."""
    b.register_known_backend(
        "wire-host",
        label="Wire Host",
        routing=b.Routing.SESSION_CONFIG,
        permission_config=("mode", "read-only"),
    )
    assert b.routing_for("wire-host") is b.Routing.SESSION_CONFIG
    assert b.permission_config_for("wire-host") == ("mode", "read-only")


# ---------------------------------------------------------------------------
# register_known_backend: the refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Acme", "acme_x", "a" * 33, "has space", "UP", "ünï"])
def test_a_malformed_id_is_refused(clean_registry, bad) -> None:
    """Ids must be lowercase ``[a-z0-9-]`` and 1-32 chars, like a descriptor id.

    A malformed id that reached the switch could start nothing, the same reason
    provider construction rejects an unknown id. (The empty string is not tested
    here: it is the kiro builtin, so it is refused as an already-known id, not as a
    malformed one -- covered by the re-registration test below.)
    """
    with pytest.raises(ValueError, match="id must match|lowercase"):
        b.register_known_backend(bad, label="X", routing=b.Routing.AGENT_SPEC)
    assert bad not in b.ACP_BACKENDS_KNOWN


@pytest.mark.parametrize("existing", ["codex", "kas", "claude", "goose"])
def test_re_registering_a_known_id_is_refused(clean_registry, existing) -> None:
    """A builtin (or already-registered) id cannot be re-registered.

    A silent overwrite of a label or routing is how one harness's registration
    clobbers another's, so it raises rather than replacing.
    """
    with pytest.raises(ValueError, match="already a known backend"):
        b.register_known_backend(existing, label="X", routing=b.Routing.AGENT_SPEC)


def test_the_empty_string_kiro_id_is_refused_as_already_known(clean_registry) -> None:
    """``""`` (kiro) is refused for FORMAT first -- it is not a registerable shape.

    The empty string fails ``[a-z0-9-]{1,32}`` before the already-known check, which
    is the honest order: an id an out-of-tree harness could author is never empty.
    """
    with pytest.raises(ValueError, match="id must match|lowercase"):
        b.register_known_backend("", label="Kiro", routing=b.Routing.AGENT_SPEC)


def test_re_registering_a_registered_id_is_refused(clean_registry) -> None:
    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    with pytest.raises(ValueError, match="already a known backend"):
        b.register_known_backend("acme", label="Acme2", routing=b.Routing.SESSION_CONFIG)


def test_session_config_without_permission_config_is_refused(clean_registry) -> None:
    """SESSION_CONFIG is enforced by applying+reading back a pair, so it is required."""
    with pytest.raises(ValueError, match="permission_config"):
        b.register_known_backend("wire-host", label="Wire", routing=b.Routing.SESSION_CONFIG)
    assert "wire-host" not in b.ACP_BACKENDS_KNOWN


def test_permission_config_on_non_session_config_routing_is_refused(clean_registry) -> None:
    """The pair is meaningful only for SESSION_CONFIG; passing it elsewhere is an error."""
    with pytest.raises(ValueError, match="only meaningful for SESSION_CONFIG"):
        b.register_known_backend(
            "acme",
            label="Acme",
            routing=b.Routing.AGENT_SPEC,
            permission_config=("mode", "read-only"),
        )


def test_an_empty_label_is_refused(clean_registry) -> None:
    with pytest.raises(ValueError, match="provider label"):
        b.register_known_backend("acme", label="", routing=b.Routing.AGENT_SPEC)


def test_a_non_routing_value_is_refused(clean_registry) -> None:
    with pytest.raises(ValueError, match="routing must be a Routing"):
        b.register_known_backend("acme", label="Acme", routing="agent_spec")  # type: ignore[arg-type]


def test_a_rejected_registration_leaves_no_partial_state(clean_registry) -> None:
    """Validation runs before any mutation, so a refusal writes nothing anywhere."""
    with pytest.raises(ValueError):
        b.register_known_backend(
            "wire-host", label="Wire", routing=b.Routing.SESSION_CONFIG  # missing config
        )
    assert "wire-host" not in b.ACP_BACKENDS_KNOWN
    assert "wire-host" not in b.ACP_BACKEND_ROUTING
    assert "wire-host" not in b.POLICY_ID_BY_BACKEND
    assert b.provider_label_for("wire-host") == ""


# ---------------------------------------------------------------------------
# The seam between "known" and "selectable" is preserved
# ---------------------------------------------------------------------------


def test_registration_alone_does_not_make_an_id_selectable(clean_registry) -> None:
    """Known is a weaker claim than selectable; registration grants only the first."""
    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    assert "acme" in b.ACP_BACKENDS_KNOWN
    assert "acme" not in b.selectable_backends()


def test_a_routed_registered_id_may_then_be_made_selectable(clean_registry) -> None:
    """AGENT_SPEC / SESSION_CONFIG routing passes ``register_selectable_backend`` unchanged.

    The UNVERIFIED refusal that function makes is not weakened: the id is admitted
    only because its recorded routing is a verified mechanism.
    """
    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    b.register_selectable_backend("acme")  # must not raise
    assert "acme" in b.selectable_backends()

    b.register_known_backend(
        "wire-host",
        label="Wire",
        routing=b.Routing.SESSION_CONFIG,
        permission_config=("mode", "read-only"),
    )
    b.register_selectable_backend("wire-host")
    assert "wire-host" in b.selectable_backends()


def test_an_unverified_registered_id_is_refused_selectability(clean_registry) -> None:
    """A registered id with UNVERIFIED routing is known but NOT selectable.

    Exactly the visible-but-unselectable state D3 wants: the id can be spelled and
    named in a rule, but ``register_selectable_backend`` refuses it because nothing
    establishes that its tool calls reach the host gate.
    """
    b.register_known_backend("no-gate", label="No Gate", routing=b.Routing.UNVERIFIED)
    assert "no-gate" in b.ACP_BACKENDS_KNOWN
    with pytest.raises(ValueError, match="routing is 'unverified'|UNVERIFIED"):
        b.register_selectable_backend("no-gate")
    assert "no-gate" not in b.selectable_backends()


def test_construction_accepts_a_registered_id_and_still_rejects_an_unknown_one(
    clean_registry, tmp_path
) -> None:
    """The membership gate reads the live view, so registration flips construction.

    Before registration ``AcpProvider(acp_backend="acme")`` raises (H8); after, it is
    accepted. An id that was never registered still raises — the gate did not go soft.
    """
    from kiro_crew.providers.acp import AcpProvider

    with pytest.raises(ValueError, match="acp_backend"):
        AcpProvider(work_dir=tmp_path, acp_backend="acme")

    b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
    provider = AcpProvider(work_dir=tmp_path, acp_backend="acme")  # must not raise
    assert provider._client.backend == "acme"

    with pytest.raises(ValueError, match="acp_backend"):
        AcpProvider(work_dir=tmp_path, acp_backend="still-unknown")


# ---------------------------------------------------------------------------
# The test-only reset
# ---------------------------------------------------------------------------


def test_reset_restores_the_frozen_builtin_state() -> None:
    """``_reset_registered_backends`` drops registered ids and their table rows.

    Not wrapped in the fixture on purpose: this test exercises the reset itself, so
    it does its own before/after bookkeeping to prove the reset — not the fixture —
    is what cleaned up.
    """
    selectable_before = set(b._selectable)
    baseline_before = set(b._baseline)
    try:
        b.register_known_backend("acme", label="Acme", routing=b.Routing.AGENT_SPEC)
        b.register_selectable_backend("acme")
        assert "acme" in b.ACP_BACKENDS_KNOWN

        b._reset_registered_backends()
        # Known set back to the eight builtins; every table row for the id gone.
        assert sorted(b.ACP_BACKENDS_KNOWN) == _BUILTINS
        assert "acme" not in b.ACP_BACKEND_ROUTING
        assert "acme" not in b.POLICY_ID_BY_BACKEND
        assert b.provider_label_for("acme") == ""
        assert b.model_registry_namespace("acme") == "acp"  # falls back to default
    finally:
        # The reset does not touch the selectable pair, so restore it by hand.
        b._selectable.clear()
        b._selectable.update(selectable_before)
        b._baseline.clear()
        b._baseline.update(baseline_before)
