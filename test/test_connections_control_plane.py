"""Contract, fault, and negative tests for the W01 control-plane seam.

The seam is pure types and zero IO, so these tests check the three things a
type-only single-source has to guarantee: the enum closed sets match the
manifest verbatim (contract), a reflected credential in an error ``detail`` is
scrubbed under the shared discipline (fault), and the additive-only /
no-credential / two-axis invariants hold (negative).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from kiro_crew import connections
from kiro_crew.connections import control_plane as cp
from kiro_crew.connections.control_plane import (
    CREDENTIAL_MODES,
    EFFECTS,
    ERROR_CLASSES,
    MAX_ERROR_CHARS,
    OPERATION_KINDS,
    RESULT_STATUSES,
    SERVICE_IDS,
    OperationContext,
    OperationDescriptor,
    OperationError,
    OperationResult,
    operation_error,
    redacted_detail,
)

# The closed sets are PARSED from the owning spec
# (connector-capability-manifest.md) rather than copied here, so a change to a
# manifest enum turns this pin red directly instead of silently diverging (the
# Design Watch advisory: this is the whole point of the module -- the manifest
# is the single source of truth, and the seam's job is to stay identical to it).
# _manifest_enum() pulls the backtick-quoted values out of the "One of ... "
# clause in a named field's table row; the ORDER preserved is the manifest's own.
_MANIFEST_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs"
    / "system-specs"
    / "modules"
    / "connector-capability-manifest.md"
)


def _manifest_enum(field: str) -> tuple[str, ...]:
    """The closed set the manifest fixes for ``field``, in the manifest's order.

    Finds the ``| `<field>` | enum | ... |`` table row, isolates the
    ``One of[:] `a`, `b`, ...`` clause, and returns the backtick-quoted tokens.
    Deliberately parsed (not hardcoded) so a manifest enum edit fails this test.
    """

    text = _MANIFEST_PATH.read_text(encoding="utf-8")
    row = next(
        (line for line in text.splitlines() if line.lstrip().startswith(f"| `{field}` | enum |")),
        None,
    )
    if row is None:
        raise AssertionError(f"no manifest table row for enum field `{field}`")
    clause = re.search(r"One of:?\s*(.+?)(?:\s+—|\.\s)", row)
    if clause is None:
        raise AssertionError(f"could not isolate the 'One of ...' clause for `{field}`")
    values = re.findall(r"`([a-z_]+)`", clause.group(1))
    if not values:
        raise AssertionError(f"no backtick-quoted values parsed for `{field}`")
    return tuple(values)


_MANIFEST_OPERATION_KINDS = _manifest_enum("operation_kind")
_MANIFEST_EFFECTS = _manifest_enum("effect")
_MANIFEST_SERVICE_IDS = _manifest_enum("service_id")
# RUN-01 is defined in the W05->W06 edge prose, not a field table row, so it is
# pinned as an in-repo literal (its owning text is the same manifest document).
_RUN01_ERROR_CLASSES = (
    "auth",
    "scope",
    "consent",
    "not_found",
    "forbidden",
    "quota",
    "throttle",
    "conflict",
    "input",
    "temporary",
    "partial",
    "ambiguous",
)
# credential_modes IS the manifest's auth_modes axis; pinned as a literal since
# auth_modes' manifest row lists it by example, not as a closed "One of" clause.
_CREDENTIAL_MODES = ("oauth_user", "fine_grained_pat", "service_to_service")

# The exact set of names ``kiro_crew.connections.__all__`` published BEFORE this
# slice, frozen here as in-repo data rather than read from a git ref. This slice
# is additive-only over the connections export face (16 in-repo modules import
# it), and freezing the baseline as a literal makes that invariant explicit and
# independent of git state -- a shallow/detached CI checkout cannot resolve
# ``origin/main``, so a ref-based baseline would fail to read (exit 128) rather
# than test anything. If a future slice legitimately adds an export, this set
# grows in the same commit; a value must never be REMOVED from it.
_BASE_CONNECTIONS_EXPORTS = frozenset(
    {
        "AUTH_MODE_DCR",
        "AUTH_MODE_PREREGISTERED",
        "CALLBACK_PATH",
        "L0_VERIFICATION_MAX_AGE_DAYS",
        "L0_VERIFICATION_WARN_AGE_DAYS",
        "AuthConfig",
        "L0Expectations",
        "Provider",
        "REGISTRY_PATH",
        "REVOKE_VERIFICATION_MAX_AGE_DAYS",
        "RegistryValidationError",
        "SmokeFixture",
        "auth_mode",
        "declared_tool_aliases",
        "derived_alias",
        "exposed_declared_tools",
        "get_all_providers",
        "get_all_registry_providers",
        "get_preregistered_providers",
        "get_provider",
        "get_tier",
        "get_visible_providers",
        "is_local_host",
        "is_preregistered",
        "natural_tool_names",
        "redirect_uri",
        "resolve_tool_aliases",
        "stale_l0_baselines",
        "statically_visible_tool_names",
    }
)


# --- Contract --------------------------------------------------------------


def test_operation_kinds_match_manifest_verbatim() -> None:
    assert OPERATION_KINDS == _MANIFEST_OPERATION_KINDS


def test_effects_match_manifest_verbatim() -> None:
    assert EFFECTS == _MANIFEST_EFFECTS


def test_service_ids_match_manifest_verbatim() -> None:
    assert SERVICE_IDS == _MANIFEST_SERVICE_IDS


def test_run01_error_classes_are_the_twelve_value_closed_set() -> None:
    assert ERROR_CLASSES == _RUN01_ERROR_CLASSES
    assert len(ERROR_CLASSES) == 12


def test_credential_mode_is_the_three_value_axis_b_set() -> None:
    assert CREDENTIAL_MODES == _CREDENTIAL_MODES


def test_result_status_success_axis_is_ok_and_partial() -> None:
    assert RESULT_STATUSES == ("ok", "partial")


def test_every_module_carries_a_schema_version_constant() -> None:
    # The precedent l0_probe/l1_smoke/status all pin a module-level version.
    assert cp.OPERATION_SCHEMA_VERSION >= 1
    assert cp.CONTEXT_SCHEMA_VERSION >= 1
    assert cp.RESULT_SCHEMA_VERSION >= 1
    assert cp.ERRORS_SCHEMA_VERSION >= 1


def test_typed_dicts_have_every_declared_field() -> None:
    descriptor: OperationDescriptor = {
        "operation_id": "github.get_rate_limit",
        "service_id": "github",
        "operation_kind": "single_fetch",
        "effect": "read",
        "credential_modes": ("oauth_user", "fine_grained_pat", "service_to_service"),
    }
    assert set(descriptor) == set(OperationDescriptor.__annotations__)

    context: OperationContext = {
        "binding_ref": "binding://gh/acct-1",
        "tenant_ref": "tenant://org-1",
        "subject_ref": "subject://user-1",
        "deadline": 1_800_000_000.0,
        "credential_mode": "oauth_user",
    }
    assert set(context) == set(OperationContext.__annotations__)

    result: OperationResult = {"status": "partial", "next_cursor": "opaque-cursor"}
    assert set(result) == set(OperationResult.__annotations__)

    error: OperationError = operation_error("throttle", "slow down")
    assert set(error) == set(OperationError.__annotations__)


def test_descriptor_declares_a_set_of_modes_call_selects_one() -> None:
    # The manifest defines auth_modes as a per-operation ARRAY, and a real
    # operation (W02's GitHub get_rate_limit) supports OAuth + PAT + s2s. The
    # descriptor must express that as a SET (credential_modes, plural); the
    # single mode a call uses lives on the per-call context (credential_mode,
    # singular) -- never a single-valued field on the descriptor.
    from typing import get_type_hints

    d_hints = get_type_hints(OperationDescriptor)
    # Descriptor carries the plural declaration set, not a singular mode.
    assert "credential_modes" in d_hints
    assert "credential_mode" not in d_hints
    # A multi-mode operation is representable on the shared seam.
    multi: OperationDescriptor = {
        "operation_id": "github.get_rate_limit",
        "service_id": "github",
        "operation_kind": "single_fetch",
        "effect": "read",
        "credential_modes": ("oauth_user", "fine_grained_pat", "service_to_service"),
    }
    assert len(multi["credential_modes"]) == 3
    # The selected mode is a per-call property, on the context, not the descriptor.
    c_hints = get_type_hints(OperationContext)
    assert "credential_mode" in c_hints
    assert "credential_modes" not in c_hints


def test_declared_modes_are_the_outer_bound_a_selection_stays_within() -> None:
    # Contract: descriptor DECLARES the permitted set; a call SELECTS from within
    # it, and a policy (e.g. L05's permit_operation) may only NARROW, never
    # permit a mode the descriptor did not declare. This test pins the shape of
    # that contract on the W01 side: the selected mode must be a member of the
    # descriptor's declared set. (L05 owns the enforcing permit_operation test.)
    descriptor: OperationDescriptor = {
        "operation_id": "github.get_rate_limit",
        "service_id": "github",
        "operation_kind": "single_fetch",
        "effect": "read",
        "credential_modes": ("oauth_user", "service_to_service"),
    }
    context: OperationContext = {
        "binding_ref": "binding://gh/acct-1",
        "tenant_ref": "tenant://org-1",
        "subject_ref": "subject://user-1",
        "deadline": 1_800_000_000.0,
        "credential_mode": "oauth_user",
    }
    assert context["credential_mode"] in descriptor["credential_modes"]
    # A mode outside the declared set is exactly what a policy must refuse; the
    # declared set is the outer bound.
    assert "fine_grained_pat" not in descriptor["credential_modes"]


# --- Fault -----------------------------------------------------------------


def test_reflected_credential_in_detail_is_redacted() -> None:
    leaked = "github pat ghp_" + "a" * 40 + " was rejected"
    error = operation_error("auth", leaked)
    assert "ghp_" + "a" * 40 not in error["detail"]
    assert error["error_class"] == "auth"


def test_detail_is_redacted_before_truncation_not_after() -> None:
    # A credential straddling the cap must not survive as a bisected prefix:
    # redaction runs over the whole string first.
    secret = "ghp_" + "z" * 40
    detail = "x" * (MAX_ERROR_CHARS - 4) + secret
    out = redacted_detail(detail)
    assert secret not in out
    assert secret[:8] not in out  # not even a bisected prefix leaks


def test_detail_is_capped_at_max_error_chars() -> None:
    out = redacted_detail("y" * 5000)
    assert len(out) <= MAX_ERROR_CHARS


def test_operation_error_never_stores_raw_detail() -> None:
    # The constructor redacts on the way in; there is no un-redacted path.
    exfil = "authorization: Bearer sk-live-" + "q" * 32
    error = operation_error("forbidden", exfil)
    assert "sk-live-" + "q" * 32 not in error["detail"]


# --- Negative --------------------------------------------------------------


def test_connections_all_is_additive_only_over_the_base() -> None:
    # Every export the base __init__ published must still be published: the 16
    # in-repo importers of kiro_crew.connections must not break. Baseline is a
    # frozen in-repo literal (see _BASE_CONNECTIONS_EXPORTS) -- not a git ref,
    # which a shallow CI checkout cannot resolve.
    now_all = set(connections.__all__)
    missing = _BASE_CONNECTIONS_EXPORTS - now_all
    assert missing == set(), f"an existing connections export was removed: {sorted(missing)}"


def test_control_plane_symbols_live_on_the_canonical_subpackage_only() -> None:
    # The control-plane symbols are consumed via the canonical
    # `kiro_crew.connections.control_plane` path (that is what W02/L02 import),
    # so they are NOT re-exported as top-level `kiro_crew.connections` aliases:
    # a second spelling with zero consumers is a rename hazard, not a
    # convenience. The subpackage itself must stay importable (it is a package,
    # not an alias), and every symbol must be reachable through it.
    from kiro_crew.connections import control_plane

    canonical_symbols = (
        "CREDENTIAL_MODES",
        "ERROR_CLASSES",
        "CredentialMode",
        "Effect",
        "ErrorClass",
        "OperationContext",
        "OperationDescriptor",
        "OperationError",
        "OperationKind",
        "OperationResult",
        "ResultStatus",
        "ServiceId",
        "operation_error",
        "redacted_detail",
    )
    for name in canonical_symbols:
        assert hasattr(control_plane, name), f"{name} missing from the canonical subpackage"
        assert name not in connections.__all__, f"{name} must not be a top-level alias"
        assert not hasattr(
            connections, name
        ), f"{name} must not be attribute-reachable at top level"


def test_registration_mode_api_is_untouched() -> None:
    # Axis A stays exactly where it was; the seam adds Axis B without moving it.
    assert connections.AUTH_MODE_DCR == "dcr"
    assert connections.AUTH_MODE_PREREGISTERED == "preregistered"
    assert callable(connections.auth_mode)
    assert callable(connections.is_preregistered)


def test_container_anchor_is_vendors_not_providers() -> None:
    import kiro_crew.connections.vendors as vendors

    assert vendors.__name__.endswith(".vendors")
    with pytest.raises(ModuleNotFoundError):
        __import__("kiro_crew.connections.providers")


def test_context_fields_are_references_typed_as_str() -> None:
    # The context fields are references / a mode identifier, never a credential
    # value: binding/tenant/subject are str refs, deadline is a float timestamp,
    # and credential_mode is a constrained mode identifier (one of the closed
    # CredentialMode set), not a token. This pins the "no credential value"
    # invariant at the type level.
    from typing import get_type_hints

    hints = get_type_hints(OperationContext)
    assert hints["binding_ref"] is str
    assert hints["tenant_ref"] is str
    assert hints["subject_ref"] is str
    assert hints["deadline"] is float
    # credential_mode is the CredentialMode Literal (a mode identifier from the
    # closed set), not an unconstrained str that could smuggle a secret.
    assert set(getattr(hints["credential_mode"], "__args__", ())) == set(CREDENTIAL_MODES)


def test_success_partial_and_error_partial_are_distinct_concepts() -> None:
    # result.partial (usable-but-incomplete success) and errors.partial
    # (a failure that partially applied) share a word, not a set.
    assert "partial" in RESULT_STATUSES
    assert "partial" in ERROR_CLASSES
    assert set(RESULT_STATUSES).isdisjoint(set(ERROR_CLASSES) - {"partial"})
