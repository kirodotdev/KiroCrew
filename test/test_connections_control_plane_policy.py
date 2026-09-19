"""Tests for the W01 · L06 five-layer governance intersection + approval binding.

Covers the six load-bearing facts the L06 contract fixes:

1. the module consumes the ONE ``SCOPE_CATALOG`` (no second catalog);
2. intersection only NARROWS — the result is ⊆ every layer's own permitted set
   and ⊆ ``resolve(platform, workspace, …)`` over the range that pair covers;
3. ceilings are inputs, not hardcoded per-layer tables;
4. approval-parameter binding — same params reuse, different params refuse
   (a positive AND a negative test);
5. deny-by-default — an unknown scope and an unknown layer refuse, not permit;
6. every refusal is a typed, redacted L01 ``OperationError``.

Plus an export guard: the new symbols are reachable via the canonical
``control_plane`` subpackage and are NOT top-level ``connections`` aliases.
"""

from __future__ import annotations

from kiro_crew import connections
from kiro_crew.connections import control_plane as cp
from kiro_crew.connections.control_plane import (
    LAYERS,
    POLICY_SCHEMA_VERSION,
    Approval,
    LayerCeilings,
    approval_applies,
    decide,
    resolve_layers,
)
from kiro_crew.connections.control_plane.policy import _fingerprint, _known_scope
from kiro_crew.platform.governance import SCOPE_CATALOG, parse_policy, parse_profile, resolve

# The scope used across the layer tests: a RULESET/identifier scope that lives
# in the shared SCOPE_CATALOG, so this module governs it without inventing a row.
_SCOPE = "tools"


def _ceiling(*, mode: str, allow=(), deny=()):
    """A GovernanceCeiling governing ``tools`` with the given allow/deny set."""

    body = {"version": 1, "boot": {"fail_closed": True}, "tools": {"mode": mode}}
    if allow:
        body["tools"]["allow"] = list(allow)
    if deny:
        body["tools"]["deny"] = list(deny)
    return parse_policy(body)


def _profile(*, mode: str, allow=(), deny=()):
    """A Profile governing ``tools`` with the given allow/deny set."""

    tools: dict = {"mode": mode}
    if allow:
        tools["allow"] = list(allow)
    if deny:
        tools["deny"] = list(deny)
    return parse_profile({"name": "p", "tools": tools})


# --- Fact 1: the ONE SCOPE_CATALOG, no second catalog ----------------------


def test_module_consumes_the_shared_scope_catalog() -> None:
    # _known_scope is a thin membership check over the imported catalog; the
    # module invents no second catalog of its own.
    from kiro_crew.connections.control_plane import policy as pol_mod

    assert pol_mod.SCOPE_CATALOG is SCOPE_CATALOG
    assert _known_scope("tools") is True
    assert _known_scope("mcp") is True
    assert _known_scope("definitely-not-a-scope") is False


def test_schema_version_and_layer_set_are_pinned() -> None:
    assert POLICY_SCHEMA_VERSION >= 1
    assert LAYERS == ("platform", "workspace", "session", "connection", "vendor")


# --- Fact 2: intersection only NARROWS -------------------------------------


def test_result_is_subset_of_every_layer_ceiling() -> None:
    # platform permits {a, b, c}; connection permits {b, c}; vendor permits {c}.
    # The intersection permits ONLY c: an item any single layer denies is denied.
    layers = LayerCeilings(
        platform=_ceiling(mode="allow", allow=["a", "b", "c"]),
        connection=_ceiling(mode="allow", allow=["b", "c"]),
        vendor=_ceiling(mode="allow", allow=["c"]),
    )
    assert resolve_layers(layers, _SCOPE, "c").permitted is True
    # b is denied by vendor; a is denied by connection AND vendor.
    assert resolve_layers(layers, _SCOPE, "b").permitted is False
    assert resolve_layers(layers, _SCOPE, "a").permitted is False


def test_adding_a_layer_can_only_shrink_the_permitted_set() -> None:
    # Without the vendor layer, b is permitted; adding a vendor layer that
    # denies b removes it. No added layer can ADD a permit.
    base = LayerCeilings(platform=_ceiling(mode="allow", allow=["a", "b"]))
    assert resolve_layers(base, _SCOPE, "b").permitted is True

    narrowed = LayerCeilings(
        platform=_ceiling(mode="allow", allow=["a", "b"]),
        vendor=_ceiling(mode="deny", deny=["b"]),
    )
    assert resolve_layers(narrowed, _SCOPE, "b").permitted is False
    # a, untouched by the vendor deny, still permitted.
    assert resolve_layers(narrowed, _SCOPE, "a").permitted is True


def test_result_is_subset_of_resolve_over_its_covered_pair() -> None:
    # Over the platform ∩ workspace range that resolve() itself covers, the
    # L06 result must never be MORE permissive than resolve()'s own answer.
    platform = _ceiling(mode="allow", allow=["a", "b", "c"])
    workspace = _profile(mode="allow", allow=["a", "b"])
    layers = LayerCeilings(platform=platform, workspace=workspace)
    for item in ("a", "b", "c", "d"):
        resolve_answer = resolve(platform, workspace, _SCOPE, item).permitted
        l06_answer = resolve_layers(layers, _SCOPE, item).permitted
        # With only these two layers the two must agree exactly (L06 delegates
        # the pair to resolve); the general invariant is l06 ⇒ resolve.
        assert l06_answer == resolve_answer
        assert (not l06_answer) or resolve_answer  # l06 permit ⇒ resolve permit


def test_a_platform_deny_is_final_regardless_of_lower_layers() -> None:
    layers = LayerCeilings(
        platform=_ceiling(mode="allow", allow=["a"]),  # only a
        connection=_ceiling(mode="allow", allow=["a", "b", "c"]),
        vendor=_ceiling(mode="allow", allow=["a", "b", "c"]),
    )
    # b is permitted by the lower layers but the platform ceiling denies it.
    dec = resolve_layers(layers, _SCOPE, "b")
    assert dec.permitted is False
    assert dec.layer == "policy"  # resolve() labels the platform deny


def test_deny_names_the_deciding_connector_layer() -> None:
    layers = LayerCeilings(
        platform=_ceiling(mode="allow", allow=["a", "b"]),
        session=_ceiling(mode="allow", allow=["a", "b"]),
        connection=_ceiling(mode="deny", deny=["b"]),
    )
    dec = resolve_layers(layers, _SCOPE, "b")
    assert dec.permitted is False
    assert dec.layer == "connection"
    assert "connection denies" in dec.reason


# --- Fact 3: ceilings are inputs, not hardcoded ----------------------------


def test_none_layer_is_ungoverned_and_permits() -> None:
    # All layers None → nothing governs → everything permits (resolve's own
    # None semantics), proving the module hardcodes no per-layer table.
    empty = LayerCeilings()
    assert resolve_layers(empty, _SCOPE, "anything").permitted is True


def test_only_supplied_ceilings_constrain() -> None:
    # The only constraint is the one the caller passed at the vendor layer.
    layers = LayerCeilings(vendor=_ceiling(mode="deny", deny=["blocked"]))
    assert resolve_layers(layers, _SCOPE, "allowed").permitted is True
    assert resolve_layers(layers, _SCOPE, "blocked").permitted is False


# --- Fact 4: approval-parameter binding (positive AND negative) ------------


def test_approval_applies_for_the_same_parameters() -> None:
    params = {"repo": "octo/hello", "ref": "main"}
    grant = Approval.grant(_SCOPE, "github.list_issues", params)
    ok, err = approval_applies(grant, _SCOPE, "github.list_issues", dict(params))
    assert ok is True
    assert err is None


def test_approval_applies_regardless_of_parameter_key_order() -> None:
    grant = Approval.grant(_SCOPE, "op", {"a": 1, "b": 2})
    ok, err = approval_applies(grant, _SCOPE, "op", {"b": 2, "a": 1})
    assert ok is True and err is None


def test_approval_does_not_apply_when_a_parameter_value_changes() -> None:
    grant = Approval.grant(_SCOPE, "github.list_issues", {"repo": "octo/hello"})
    ok, err = approval_applies(grant, _SCOPE, "github.list_issues", {"repo": "octo/other"})
    assert ok is False
    assert err is not None
    assert err["error_class"] == "forbidden"


def test_approval_does_not_apply_when_a_parameter_is_added() -> None:
    grant = Approval.grant(_SCOPE, "op", {"a": 1})
    ok, err = approval_applies(grant, _SCOPE, "op", {"a": 1, "b": 2})
    assert ok is False and err is not None


def test_approval_does_not_apply_to_a_different_item() -> None:
    grant = Approval.grant(_SCOPE, "op-a", {"a": 1})
    ok, err = approval_applies(grant, _SCOPE, "op-b", {"a": 1})
    assert ok is False and err is not None


def test_fingerprint_is_order_independent_and_value_sensitive() -> None:
    assert _fingerprint({"a": 1, "b": 2}) == _fingerprint({"b": 2, "a": 1})
    assert _fingerprint({"a": 1}) != _fingerprint({"a": 2})
    assert _fingerprint({"a": 1}) != _fingerprint({"a": 1, "b": 2})


# --- Fact 5: deny-by-default -----------------------------------------------


def test_unknown_scope_is_denied_by_default() -> None:
    ok, err = decide(LayerCeilings(), "not-a-real-scope", "x")
    assert ok is False
    assert err is not None
    assert err["error_class"] == "forbidden"


def test_known_scope_with_no_governance_is_permitted() -> None:
    # deny-by-default applies to UNKNOWN scopes; a known scope with no governing
    # layer is ungoverned-and-permitted, matching resolve()'s semantics.
    ok, err = decide(LayerCeilings(), _SCOPE, "x")
    assert ok is True and err is None


def test_decide_returns_typed_refusal_on_a_governed_deny() -> None:
    layers = LayerCeilings(platform=_ceiling(mode="allow", allow=["a"]))
    ok, err = decide(layers, _SCOPE, "b")
    assert ok is False
    assert err is not None
    assert err["error_class"] == "forbidden"


# --- Fact 6: refusals are typed AND redacted -------------------------------


def test_refusal_detail_is_redacted() -> None:
    # A leaked-looking token in the item name must be scrubbed out of the typed
    # refusal detail — decide() builds it with operation_error (unconditional
    # redaction), never a hand-assembled string.
    secret = "ghp_" + "a" * 40
    ok, err = decide(LayerCeilings(), "not-a-real-scope", secret)
    assert ok is False and err is not None
    assert secret not in err["detail"]


def test_approval_refusal_detail_is_a_typed_operation_error() -> None:
    grant = Approval.grant(_SCOPE, "op", {"token": "ghp_" + "z" * 40})
    ok, err = approval_applies(grant, _SCOPE, "op", {"token": "ghp_" + "y" * 40})
    assert ok is False and err is not None
    # It is a typed OperationError with both required fields present.
    assert set(err) == {"error_class", "detail"}


# --- Export guard: canonical subpackage only, no top-level alias -----------


def test_l06_symbols_live_on_the_canonical_subpackage_only() -> None:
    # Mirrors test_connections_control_plane's guard: the new symbols are
    # reachable through kiro_crew.connections.control_plane, and are NOT
    # re-exported as top-level kiro_crew.connections aliases (a zero-consumer
    # second spelling is a rename hazard, which is why L01's 14 and L02's 9 were
    # deleted). The subpackage stays a real package, not an alias.
    from kiro_crew.connections import control_plane

    l06_symbols = (
        "LAYERS",
        "POLICY_SCHEMA_VERSION",
        "Approval",
        "LayerCeilings",
        "LayerName",
        "approval_applies",
        "decide",
        "resolve_layers",
    )
    for name in l06_symbols:
        assert hasattr(control_plane, name), f"{name} missing from the canonical subpackage"
        assert name in control_plane.__all__, f"{name} must be exported by control_plane"
        assert name not in connections.__all__, f"{name} must not be a top-level alias"
        assert not hasattr(
            connections, name
        ), f"{name} must not be attribute-reachable at top level"


def test_policy_module_carries_a_schema_version() -> None:
    assert cp.POLICY_SCHEMA_VERSION >= 1
