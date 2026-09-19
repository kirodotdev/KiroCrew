"""Contract, deny-by-default, redaction, and axis-orthogonality tests for the
W01 · L05 per-operation credential-mode gate (``control_plane/auth_modes.py``).

The module is pure types + ONE authorization decision (:func:`permit_operation`,
which consults BOTH the operation descriptor and the policy) with zero IO, so
these tests pin the facts the slice owes: (1) declarations are pinned to the L01
closed set and cannot mint a new mode; (2) authorization allows a declared+
permitted mode and denies otherwise, returning the L01 typed error; (3) a mode
the descriptor did not declare is DENIED even if the policy permits it (the
descriptor is the outer bound); (4) an UNKNOWN mode outside the closed set is
denied even if descriptor and policy both carry it (closed-set floor); (5)
deny-by-default; (6) the API carries only mode identifiers and references, never
a credential value and never leaks one. Structural guards ride along: the two
"auth mode" axes are DISJOINT; the public symbols are reachable via the canonical
``control_plane`` subpackage but are NOT top-level aliases; and there is no
public policy-only authz bypass.
"""

from __future__ import annotations

import pytest

from kiro_crew import connections
from kiro_crew.connections import control_plane as cp
from kiro_crew.connections import registry
from kiro_crew.connections.control_plane import (
    CREDENTIAL_MODES,
    OperationDescriptor,
    OperationError,
    PermittedModeRegistry,
    PermittedModes,
    declare_permitted_modes,
    effective_permitted_modes,
    permit_operation,
    permit_registered_operation,
)


def _descriptor(operation_id: str, credential_modes: tuple[str, ...]) -> OperationDescriptor:
    """Build an OperationDescriptor whose credential_modes is the declared set.

    The other fields are fixed valid closed-set values; this slice's tests only
    exercise operation_id + credential_modes (the declared outer bound).
    """

    return {
        "operation_id": operation_id,
        "service_id": "github",
        "operation_kind": "list",
        "effect": "read",
        "credential_modes": credential_modes,  # type: ignore[typeddict-item]
    }


# The Axis-B closed set as the owning spec (connector-capability-manifest.md's
# per-operation ``auth_modes``) fixes it. Duplicated here on purpose: a test
# that imported the same tuple it checks would pass even if a value were
# silently dropped from both.
_MANIFEST_AUTH_MODES = ("oauth_user", "fine_grained_pat", "service_to_service")

# The Axis-A closed set (registration mode), likewise duplicated as literals.
_REGISTRATION_MODES = ("dcr", "preregistered")


# --- Contract: pinned to the L01 closed set, no new words ------------------


def test_declared_modes_match_the_l01_axis_b_closed_set_verbatim() -> None:
    assert tuple(CREDENTIAL_MODES) == _MANIFEST_AUTH_MODES


def test_declare_permitted_modes_returns_an_immutable_frozenset() -> None:
    permitted = declare_permitted_modes(["oauth_user", "fine_grained_pat"])
    assert isinstance(permitted, frozenset)
    assert permitted == {"oauth_user", "fine_grained_pat"}


def test_declare_permitted_modes_rejects_a_mode_outside_the_closed_set() -> None:
    # A fourth, invented credential mode is a programming error, not a silent
    # widening of the closed set.
    with pytest.raises(ValueError) as excinfo:
        declare_permitted_modes(["oauth_user", "device_code"])  # type: ignore[list-item]
    assert "device_code" in str(excinfo.value)


def test_declare_permitted_modes_accepts_the_empty_declaration() -> None:
    # An empty declaration is legal and means "nothing permitted" -- the
    # deny-by-default base case, not an error.
    assert declare_permitted_modes([]) == frozenset()


def test_a_full_declaration_covers_every_l01_mode() -> None:
    # A descriptor that declares every mode, with a policy permitting every
    # mode, authorizes each one.
    descriptor = _descriptor("gh.op", CREDENTIAL_MODES)
    permitted = declare_permitted_modes(CREDENTIAL_MODES)
    for mode in _MANIFEST_AUTH_MODES:
        assert permit_operation(descriptor, mode, permitted) is None  # type: ignore[arg-type]


# --- Authorization: allow a declared+permitted mode, deny otherwise --------


def test_permit_operation_allows_a_declared_and_permitted_mode() -> None:
    descriptor = _descriptor("github.list_issues", ("oauth_user", "service_to_service"))
    permitted = declare_permitted_modes(["oauth_user", "service_to_service"])
    assert permit_operation(descriptor, "oauth_user", permitted) is None
    assert permit_operation(descriptor, "service_to_service", permitted) is None


def test_permit_operation_denies_an_unpermitted_mode_with_the_l01_typed_error() -> None:
    # Declared by the descriptor but narrowed out by the policy.
    descriptor = _descriptor("github.list_issues", ("oauth_user", "service_to_service"))
    permitted = declare_permitted_modes(["oauth_user"])
    error = permit_operation(descriptor, "service_to_service", permitted)
    assert error is not None
    # It is the L01 OperationError shape, RUN-01 class ``auth``.
    assert set(error) == set(OperationError.__annotations__)
    assert error["error_class"] == "auth"
    # The detail names the operation and the mode identifiers (closed-set,
    # non-secret), so a governance reader can see WHY without any credential.
    assert "service_to_service" in error["detail"]
    assert "github.list_issues" in error["detail"]


# --- deny-by-default (each pinned by its own assertion) --------------------


def test_empty_policy_denies_every_mode() -> None:
    descriptor = _descriptor("svc.op", CREDENTIAL_MODES)
    empty: PermittedModes = declare_permitted_modes([])
    for mode in _MANIFEST_AUTH_MODES:
        error = permit_operation(descriptor, mode, empty)  # type: ignore[arg-type]
        assert error is not None
        assert error["error_class"] == "auth"


def test_operation_absent_from_registry_is_denied_not_allowed() -> None:
    # The load-bearing deny-by-default case: a registry is the complete
    # statement of what is permitted; an unstated operation resolves to the
    # empty set, so every offered mode is denied.
    descriptor = _descriptor("github.delete_repo", ("oauth_user",))
    registry_map: PermittedModeRegistry = {
        "github.list_issues": declare_permitted_modes(["oauth_user"]),
    }
    error = permit_registered_operation(descriptor, "oauth_user", registry_map)
    assert error is not None
    assert error["error_class"] == "auth"


def test_registry_lookup_allows_only_a_declared_permitted_mode() -> None:
    descriptor = _descriptor("gh.op", ("oauth_user", "fine_grained_pat"))
    registry_map: PermittedModeRegistry = {
        "gh.op": declare_permitted_modes(["fine_grained_pat"]),
    }
    assert permit_registered_operation(descriptor, "fine_grained_pat", registry_map) is None
    assert permit_registered_operation(descriptor, "oauth_user", registry_map) is not None


# --- Zero credential values: no token/secret is accepted or leaked ---------


def test_denied_detail_never_leaks_a_reflected_credential() -> None:
    # operation_error redacts unconditionally, so even if an operation_id
    # reference somehow carried a token-shaped substring it is scrubbed. The
    # API never takes a credential value in the first place; this pins that a
    # deny detail cannot become an exfil channel either.
    descriptor = _descriptor("op.with.ghp_" + "a" * 40, ("oauth_user",))
    permitted = declare_permitted_modes(["oauth_user"])
    error = permit_operation(descriptor, "service_to_service", permitted)
    assert error is not None
    assert "ghp_" + "a" * 40 not in error["detail"]


# --- Axis orthogonality guard: the two "auth mode" value sets are disjoint --


def test_the_two_auth_mode_axes_have_disjoint_value_sets() -> None:
    # Axis A (registration mode) and Axis B (credential mode) share the word
    # "auth mode" and NOTHING else. A disjointness assertion guards the collapse
    # the manifest's two-axis design forbids better than prose does.
    axis_a = {registry.AUTH_MODE_DCR, registry.AUTH_MODE_PREREGISTERED}
    axis_b = set(CREDENTIAL_MODES)
    assert axis_a == set(_REGISTRATION_MODES)
    assert axis_b == set(_MANIFEST_AUTH_MODES)
    assert axis_a.isdisjoint(axis_b)


def test_l05_does_not_import_or_touch_the_registration_mode_api() -> None:
    # Axis A stays exactly where it was; L05 adds the Axis-B gate without moving
    # or redefining any registry API.
    assert registry.AUTH_MODE_DCR == "dcr"
    assert registry.AUTH_MODE_PREREGISTERED == "preregistered"
    assert callable(registry.auth_mode)
    assert callable(registry.is_preregistered)


# --- Export discipline: canonical subpackage only, not a top-level alias ----


def test_l05_symbols_live_on_the_canonical_subpackage_only() -> None:
    # The public symbols are consumed via kiro_crew.connections.control_plane
    # (the canonical path W02..W14 import). They are NOT re-exported as top-level
    # kiro_crew.connections aliases: a second spelling with zero consumers is a
    # rename hazard, not a convenience -- the exact thing First Principles made
    # L01 and L02 delete.
    l05_symbols = (
        "AUTH_MODES_SCHEMA_VERSION",
        "PermittedModeRegistry",
        "PermittedModes",
        "declare_permitted_modes",
        "effective_permitted_modes",
        "permit_operation",
        "permit_registered_operation",
    )
    for name in l05_symbols:
        assert hasattr(cp, name), f"{name} missing from the canonical subpackage"
        assert name in cp.__all__, f"{name} must be exported from control_plane.__all__"
        assert name not in connections.__all__, f"{name} must not be a top-level alias"
        assert not hasattr(
            connections, name
        ), f"{name} must not be attribute-reachable at top level"


def test_there_is_no_public_policy_only_authz_bypass() -> None:
    # Problem-1 guard: the ONLY authorization decision is permit_operation /
    # permit_registered_operation, and both REQUIRE a descriptor. The old
    # policy-only spellings and the folded descriptor-aware aliases must not be
    # public, and the bare policy-membership predicate must be private -- a
    # public function that looked like authz but skipped the descriptor would be
    # a bypass.
    for banned in (
        "permit_declared_operation",
        "permit_declared_registered_operation",
        "is_mode_permitted",
    ):
        assert banned not in cp.__all__, f"{banned} must not be a public control_plane export"
        assert not hasattr(cp, banned), f"{banned} must not be attribute-reachable on control_plane"
    # The private predicate exists on the module but only under its underscore
    # name, and is not exported.
    from kiro_crew.connections.control_plane import auth_modes

    assert hasattr(auth_modes, "_is_mode_permitted")
    assert "_is_mode_permitted" not in getattr(auth_modes, "__all__", [])


def test_module_carries_a_schema_version_constant() -> None:
    # Matches the precedent every sibling control-plane module sets.
    assert cp.AUTH_MODES_SCHEMA_VERSION >= 1


# --- Descriptor is the outer bound: "descriptor declares, policy narrows" ---
# The judgment point of this slice. The descriptor's credential_modes is the
# complete statement of what the operation supports; a policy may only subtract.
# These prove the enforcement is REAL CODE (an intersection in
# effective_permitted_modes / permit_operation), not a TypedDict docstring --
# a TypedDict validates nothing at runtime.


def test_effective_permitted_modes_is_policy_intersect_declared() -> None:
    # A policy that names a mode the descriptor did NOT declare cannot widen the
    # declared set: the effective set is the intersection, so the undeclared
    # mode is dropped.
    descriptor = _descriptor("gh.list_issues", ("oauth_user", "fine_grained_pat"))
    over_permissive = declare_permitted_modes(
        ["oauth_user", "fine_grained_pat", "service_to_service"]
    )
    effective = effective_permitted_modes(descriptor, over_permissive)
    assert effective == {"oauth_user", "fine_grained_pat"}
    assert "service_to_service" not in effective  # descriptor never declared it


def test_undeclared_mode_is_denied_EVEN_WHEN_the_registry_permits_it() -> None:
    # THE counter-example. The descriptor declares only oauth_user. The registry
    # (a policy) permits service_to_service too -- an over-permissive policy. The
    # offered mode service_to_service must STILL be denied, because the
    # descriptor did not declare it: policy cannot permit past the descriptor.
    descriptor = _descriptor("gh.list_issues", ("oauth_user",))
    registry_map: PermittedModeRegistry = {
        "gh.list_issues": declare_permitted_modes(["oauth_user", "service_to_service"]),
    }
    # NEGATIVE side: the undeclared-but-registry-permitted mode is denied.
    error = permit_registered_operation(descriptor, "service_to_service", registry_map)
    assert error is not None, "an undeclared mode must be denied even if the registry permits it"
    assert error["error_class"] == "auth"
    assert "not DECLARED" in error["detail"]
    assert "service_to_service" in error["detail"]
    # POSITIVE side: a declared-AND-permitted mode is allowed.
    assert permit_registered_operation(descriptor, "oauth_user", registry_map) is None


def test_permit_operation_allows_only_declared_and_permitted() -> None:
    # Positive: declared + permitted -> allow.
    descriptor = _descriptor("gh.op", ("oauth_user", "fine_grained_pat"))
    permitted = declare_permitted_modes(["oauth_user", "fine_grained_pat"])
    assert permit_operation(descriptor, "oauth_user", permitted) is None
    assert permit_operation(descriptor, "fine_grained_pat", permitted) is None


def test_declared_but_unpermitted_mode_is_denied_with_a_distinct_reason() -> None:
    # A mode the descriptor DID declare but the policy did NOT permit is denied
    # too (policy narrows within the bound) -- and the deny reason is distinct
    # from the undeclared case.
    descriptor = _descriptor("gh.op", ("oauth_user", "fine_grained_pat"))
    permitted = declare_permitted_modes(["oauth_user"])  # narrows to one
    error = permit_operation(descriptor, "fine_grained_pat", permitted)
    assert error is not None
    assert error["error_class"] == "auth"
    assert "not permitted by policy" in error["detail"]
    assert "not DECLARED" not in error["detail"]


def test_descriptor_bound_still_deny_by_default_on_empty_declaration() -> None:
    # An operation that declares NO mode denies everything, regardless of policy.
    descriptor = _descriptor("gh.unconfigured", ())
    over_permissive = declare_permitted_modes(CREDENTIAL_MODES)
    for mode in _MANIFEST_AUTH_MODES:
        error = permit_operation(descriptor, mode, over_permissive)  # type: ignore[arg-type]
        assert error is not None
        assert error["error_class"] == "auth"


def test_descriptor_bound_deny_detail_never_leaks_a_credential() -> None:
    # The descriptor-aware deny path also routes through operation_error, so a
    # token-shaped operation_id substring is scrubbed.
    descriptor = _descriptor("op.ghp_" + "a" * 40, ("oauth_user",))
    permitted = declare_permitted_modes(["oauth_user"])
    error = permit_operation(descriptor, "service_to_service", permitted)
    assert error is not None
    assert "ghp_" + "a" * 40 not in error["detail"]


# --- Unknown mode must be denied even when descriptor AND policy both carry it -
# The closed-set discipline: an intersection alone is not enough. If a descriptor
# declares an out-of-closed-set mode and a policy permits the same string, the
# intersection is non-empty and a naive check would ALLOW it -- violating
# "unknown mode is always denied". A descriptor is a TypedDict (no runtime
# validation), so a bogus credential_modes value can reach the gate; the gate
# must intersect with CREDENTIAL_MODES too.


def test_unknown_mode_denied_even_if_descriptor_and_policy_both_carry_it() -> None:
    bogus = "totally_bogus_mode"
    # A descriptor whose declared set contains a value outside the closed set
    # (a TypedDict does not stop this), and a policy built to permit the same
    # bogus string (bypassing declare_permitted_modes' own closed-set guard).
    descriptor = _descriptor("gh.op", ("oauth_user", bogus))
    over_permissive: PermittedModes = frozenset({"oauth_user", bogus})  # type: ignore[arg-type]
    error = permit_operation(descriptor, bogus, over_permissive)  # type: ignore[arg-type]
    assert error is not None, "an unknown credential mode must be denied, never allowed"
    assert error["error_class"] == "auth"
    # A known, declared, permitted mode on the same descriptor still works.
    assert permit_operation(descriptor, "oauth_user", over_permissive) is None


def test_effective_permitted_modes_excludes_unknown_modes() -> None:
    bogus = "totally_bogus_mode"
    descriptor = _descriptor("gh.op", ("oauth_user", bogus))
    over_permissive: PermittedModes = frozenset({"oauth_user", bogus})  # type: ignore[arg-type]
    effective = effective_permitted_modes(descriptor, over_permissive)
    assert bogus not in effective, "the effective set must never contain an out-of-closed-set mode"
    assert effective == {"oauth_user"}
