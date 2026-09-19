"""Contract, fault, and negative tests for the W01 · L08 derived handle.

The handle is a caller-held CLAIM; the issuance record is the authority. These
tests check the three invariants the slice exists to enforce, each with a REAL
refusal from REAL code (never a docstring): (1) scope only narrows -- both at
mint AND at use, so a caller who widens the handle dict is refused; (2) an
expired handle is refused, decided from the trusted record, and a non-finite TTL
is refused at mint; (3) the handle carries nothing that reconstructs the
binding. They also pin the cross-process / restart contract (fail closed) and
that the new symbols are canonical to ``control_plane`` and NOT top-level
aliases.
"""

from __future__ import annotations

import pytest

from kiro_crew import connections
from kiro_crew.connections import control_plane as cp
from kiro_crew.connections.control_plane import (
    Binding,
    DerivedHandle,
    HandleExpiredError,
    HandleNotIssuedError,
    HandleScopeError,
    HandleTamperedError,
    TrustedHandleView,
    VerifiedIdentity,
    create_binding,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane import handle as handle_mod
from kiro_crew.connections.control_plane import (
    is_expired,
    next_generation,
)
from kiro_crew.connections.control_plane.errors import ERROR_INPUT, ERROR_SCOPE
from kiro_crew.connections.control_plane.handle import _binding_fingerprint

# A fixed clock for the TTL tests, so nothing here reads the wall clock.
_T0 = 1_800_000_000.0


def _accepting_verifier(*, claimed_subject, claimed_tenant, service_id) -> VerifiedIdentity:
    """Verify the claim and normalize it to canonical refs (distinct strings)."""

    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _make_binding() -> Binding:
    return create_binding(
        service_id="outlook",
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_accepting_verifier,
        slug="outlook",
    )


_GRANTED = ("mail.read", "mail.send", "calendars.read")


def _make_handle(
    *,
    binding: Binding | None = None,
    requested=("mail.read",),
    now: float = _T0,
    ttl_seconds: float = 300.0,
) -> DerivedHandle:
    return derive_handle(
        binding or _make_binding(),
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=now,
        ttl_seconds=ttl_seconds,
    )


# --- Contract --------------------------------------------------------------


def test_handle_has_a_schema_version() -> None:
    assert cp.HANDLE_SCHEMA_VERSION >= 1


def test_handle_typed_dict_has_every_declared_field() -> None:
    handle = _make_handle()
    assert set(handle) == set(DerivedHandle.__annotations__)


def test_derived_scopes_are_a_subset_of_granted_and_are_canonicalized() -> None:
    handle = _make_handle(requested=("mail.send", "mail.read", "mail.read"))
    assert handle["scopes"] == ("mail.read", "mail.send")
    assert set(handle["scopes"]).issubset(set(_GRANTED))


def test_handle_carries_service_and_credential_mode_for_the_consumer() -> None:
    binding = _make_binding()
    handle = _make_handle(binding=binding)
    assert handle["service_id"] == binding["service_id"]
    assert handle["credential_mode"] == binding["credential_mode"]


def test_ttl_sets_absolute_issued_at_and_not_after() -> None:
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    assert handle["issued_at"] == _T0
    assert handle["not_after"] == _T0 + 300.0


def test_generation_is_carried_onto_the_handle_for_l04_fencing() -> None:
    binding = next_generation(next_generation(_make_binding()))
    assert binding["generation"] == 3
    handle = _make_handle(binding=binding)
    assert handle["generation"] == 3


def test_a_still_live_handle_is_usable() -> None:
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    assert is_expired(handle, now=_T0 + 299.0) is False
    ensure_usable(handle, now=_T0 + 299.0)  # returns None, does not raise


# --- Fault: scope narrowing (mint AND use) ---------------------------------


def test_a_scope_not_held_by_the_binding_is_refused_not_silently_dropped() -> None:
    # Invariant 1 at MINT. The widening attempt is refused by real code, not
    # narrowed away: derive_handle raises, no handle is returned.
    with pytest.raises(HandleScopeError) as excinfo:
        derive_handle(
            _make_binding(),
            granted_scopes=_GRANTED,
            requested_scopes=("mail.read", "files.readwrite.all"),
            now=_T0,
            ttl_seconds=300.0,
        )
    err = excinfo.value.error
    assert err["error_class"] == ERROR_SCOPE
    assert "files.readwrite.all" in err["detail"]


def test_an_over_ask_yields_no_handle_at_all_the_grant_is_not_narrowed() -> None:
    with pytest.raises(HandleScopeError):
        _make_handle(requested=("calendars.readwrite",))


def test_a_caller_widened_scope_is_refused_even_though_the_handle_says_so() -> None:
    # Invariant 1 at USE. The DerivedHandle is a mutable dict; a caller widens
    # its scope after minting. ensure_usable judges against the trusted record,
    # not the handle's own field, so the widening is REFUSED -- not obeyed.
    handle = _make_handle(requested=("mail.read",), now=_T0, ttl_seconds=300.0)
    handle["scopes"] = ("mail.read", "mail.send", "files.readwrite.all")  # caller widens
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_a_caller_extended_expiry_is_refused() -> None:
    # Invariant 2 at USE. A caller pushes not_after far into the future on the
    # dict. ensure_usable enforces the RECORD's expiry, and the claim being later
    # than the record is itself a tamper refusal -- pushing the field cannot buy
    # more life.
    handle = _make_handle(now=_T0, ttl_seconds=100.0)
    handle["not_after"] = _T0 + 10**9  # caller pushes expiry out
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 500.0)  # 500s > issued 100s TTL
    assert excinfo.value.error["error_class"] == "auth"


def test_a_caller_changed_generation_is_refused() -> None:
    # A caller changes the generation on the dict; ensure_usable requires it to
    # match the record and refuses the mismatch.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["generation"] = 999
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


# --- Fault: malformed shape (F3) -------------------------------------------
# A handle's fields are UNTRUSTED, so a missing/corrupt field is the SAME class
# of input as a widened scope and must be a TYPED refusal -- not an uncaught
# KeyError/TypeError that carries no error_class, skips the audit, and jumps the
# decision chain onto the caller. Each test below first proves the OLD behavior
# would crash (the raw exception the bare read raised) and asserts the fix turns
# it into HandleTamperedError.


def test_a_handle_missing_scopes_is_refused_not_crashed() -> None:
    # Was: `set(handle["scopes"])` raised an uncaught KeyError: 'scopes'.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    del handle["scopes"]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"
    assert "scopes" in excinfo.value.error["detail"]


def test_a_handle_with_a_non_iterable_scopes_is_refused_not_crashed() -> None:
    # Was: `set(12345)` raised an uncaught TypeError: 'int' object is not iterable.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["scopes"] = 12345  # type: ignore[typeddict-item]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_a_handle_with_unhashable_scope_elements_is_refused_not_crashed() -> None:
    # Was: `set([["mail.read"]])` raised an uncaught TypeError: unhashable type.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["scopes"] = [["mail.read"]]  # type: ignore[list-item]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_a_handle_missing_expiry_is_refused_not_crashed() -> None:
    # Was: `handle["not_after"] > record.not_after` raised KeyError: 'not_after'.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    del handle["not_after"]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"
    assert "not_after" in excinfo.value.error["detail"]


def test_a_handle_with_a_non_numeric_expiry_is_refused_not_crashed() -> None:
    # Was: `"soon" > record.not_after` raised TypeError: '>' not supported
    # between instances of 'str' and 'float'.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["not_after"] = "soon"  # type: ignore[typeddict-item]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_a_handle_missing_handle_id_is_refused_not_crashed() -> None:
    # The shape gate runs BEFORE the registry lookup, so even a missing
    # handle_id (which _record_for would KeyError on) is a typed refusal.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    del handle["handle_id"]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"
    assert "handle_id" in excinfo.value.error["detail"]


def test_a_non_mapping_handle_is_refused_not_crashed() -> None:
    # A caller can pass something that is not a dict at all; refuse it typed.
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(None, now=_T0 + 1.0)  # type: ignore[arg-type]
    assert excinfo.value.error["error_class"] == "auth"


def test_the_shape_gate_names_only_the_field_never_its_value() -> None:
    # The refusal detail must not echo the offending value (it goes through L01
    # redaction regardless, but it should name only the field).
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["binding_fingerprint"] = 12345  # type: ignore[typeddict-item]
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    detail = excinfo.value.error["detail"]
    assert "binding_fingerprint" in detail
    assert "12345" not in detail


# --- Fault: is_expired shares the same crash surface, but fails CLOSED ------
# is_expired is a boolean PREDICATE (its contract: the typed refusal belongs to
# ensure_usable). The registry lookup it shares with ensure_usable ends in a
# bare handle["handle_id"], so a malformed handle would crash it the same way.
# A malformed handle is a WEAKER case than "unknown", so under the same
# fail-closed contract it must answer True (expired) -- never raise.


def test_is_expired_on_a_handle_missing_handle_id_is_true_not_crashed() -> None:
    # Was: _record_for's `_ISSUED.get(handle["handle_id"])` raised KeyError.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    del handle["handle_id"]
    assert is_expired(handle, now=_T0 + 1.0) is True


def test_is_expired_on_a_handle_with_unhashable_handle_id_is_true_not_crashed() -> None:
    # Was: `_ISSUED.get([])` raised TypeError: unhashable type: 'list'.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["handle_id"] = []  # type: ignore[typeddict-item]
    assert is_expired(handle, now=_T0 + 1.0) is True


# --- Fault: TTL finiteness -------------------------------------------------


def test_a_non_finite_ttl_is_refused() -> None:
    # Invariant 2 at MINT. NaN slips past a bare `<= 0` (NaN comparisons are
    # always False, so a NaN expiry would never look expired) and inf gives a
    # never-expiring handle. Both are refused before a handle exists.
    for bad_ttl in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            _make_handle(ttl_seconds=bad_ttl)
    # And a now + ttl that overflows to inf is refused too.
    with pytest.raises(ValueError):
        _make_handle(now=1.0e308, ttl_seconds=1.0e308)


def test_an_expired_handle_is_refused() -> None:
    # Invariant 2 at USE, honest handle. Decided from the record's not_after,
    # inclusive; the typed reason says it expired.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    assert is_expired(handle, now=_T0 + 300.0) is True
    with pytest.raises(HandleExpiredError) as excinfo:
        ensure_usable(handle, now=_T0 + 300.0)
    err = excinfo.value.error
    assert err["error_class"] == ERROR_INPUT
    assert "expired" in err["detail"]


def test_expiry_refusal_is_driven_by_the_record_not_the_handle_field() -> None:
    # The SAME honest handle is usable before its cutoff and refused after it,
    # and the decision does not read the handle's own not_after (that is proven
    # by test_a_caller_extended_expiry_is_refused). Here the handle is untouched.
    handle = _make_handle(now=_T0, ttl_seconds=100.0)
    ensure_usable(handle, now=_T0 + 99.0)  # usable
    with pytest.raises(HandleExpiredError):
        ensure_usable(handle, now=_T0 + 100.0)  # refused, just past the cutoff


# --- Fault: non-finite verification clock; routing-axis tamper; trusted view ---


def test_a_non_finite_now_is_refused() -> None:
    # Defect 4. A NaN `now` makes `now >= not_after` False, which would let an
    # EXPIRED handle through -- the expiry check disabled by the caller's clock.
    # ensure_usable refuses a non-finite `now` as its first step (typed input),
    # so the clock is never trusted unchecked.
    handle = _make_handle(now=_T0, ttl_seconds=100.0)  # expired at _T0+100
    for bad_now in (float("nan"), float("inf")):
        with pytest.raises(HandleExpiredError) as excinfo:
            ensure_usable(handle, now=bad_now)
        assert excinfo.value.error["error_class"] == ERROR_INPUT


def test_a_caller_changed_service_id_is_refused() -> None:
    # Defect 5. A caller flips service_id on the dict to redirect the call to a
    # different provider. ensure_usable compares against the record and refuses
    # the mismatch (typed auth) -- a Graph handle cannot be pointed at github.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    assert handle["service_id"] == "outlook"
    handle["service_id"] = "github"
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_a_caller_changed_credential_mode_is_refused() -> None:
    # Defect 5. A caller swaps credential_mode on the dict. Refused against the
    # record (typed auth) -- the auth axis is not caller-choosable.
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    assert handle["credential_mode"] == "oauth_user"
    handle["credential_mode"] = "service_to_service"
    with pytest.raises(HandleTamperedError) as excinfo:
        ensure_usable(handle, now=_T0 + 1.0)
    assert excinfo.value.error["error_class"] == "auth"


def test_the_returned_view_comes_from_the_record_not_the_handle() -> None:
    # Defect 5, mechanical proof that the returned view is RECORD-sourced. Most
    # routing fields have an exact-match tamper check, so changing them is
    # refused before a view is built. not_after is checked with `>` (a claim
    # LATER than the record is refused; an EARLIER claim is allowed), and scopes
    # with subset, so we set the handle's not_after EARLIER and its scopes
    # NARROWER than the record -- both pass the tamper checks -- and confirm the
    # view still reports the RECORD's later expiry and full scope set. If the
    # view read the handle, it would report the shrunken copies instead.
    binding = _make_binding()
    handle = _make_handle(
        binding=binding, requested=("mail.read", "mail.send"), now=_T0, ttl_seconds=300.0
    )
    record_not_after = _T0 + 300.0
    handle["not_after"] = _T0 + 10.0  # caller claims LESS (earlier) -- allowed
    handle["scopes"] = ("mail.read",)  # caller claims LESS (subset) -- allowed
    view = ensure_usable(handle, now=_T0 + 20.0)  # past handle's claim, before record's
    assert isinstance(view, TrustedHandleView)
    # The view reports the RECORD's values, not the handle's shrunken copies.
    assert view.not_after == record_not_after  # record's, not the handle's _T0+10
    assert view.scopes == ("mail.read", "mail.send")  # record's full set, not the narrowed claim
    assert view.service_id == binding["service_id"] == "outlook"
    assert view.credential_mode == binding["credential_mode"] == "oauth_user"
    assert view.generation == binding["generation"]
    # The view is frozen: a consumer cannot mutate the trusted answer.
    with pytest.raises(Exception):
        view.service_id = "github"  # type: ignore[misc]


def test_a_usable_handle_returns_a_trusted_view() -> None:
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    view = ensure_usable(handle, now=_T0 + 10.0)
    assert isinstance(view, TrustedHandleView)
    assert view.handle_id == handle["handle_id"]


# --- Fault: cross-process / restart (fail closed) --------------------------


def test_a_handle_from_before_restart_is_refused() -> None:
    # The issuance registry is process-local and in-memory; a restart starts it
    # empty. Simulate a restart by clearing the registry AFTER minting: the
    # handle's id does not resolve, so ensure_usable fails closed with a typed
    # auth refusal (NOT silently accepted, NOT treated as valid).
    handle = _make_handle(now=_T0, ttl_seconds=10_000.0)
    ensure_usable(handle, now=_T0 + 1.0)  # usable before the "restart"
    saved = dict(handle_mod._ISSUED)
    handle_mod._ISSUED.clear()  # <- restart: fresh, empty registry
    try:
        assert is_expired(handle, now=_T0 + 1.0) is True  # fail closed
        with pytest.raises(HandleNotIssuedError) as excinfo:
            ensure_usable(handle, now=_T0 + 1.0)
        assert excinfo.value.error["error_class"] == "auth"
    finally:
        handle_mod._ISSUED.clear()
        handle_mod._ISSUED.update(saved)


def test_a_handle_never_issued_by_this_process_is_refused() -> None:
    # A hand-built handle whose id was never issued here (the cross-process case:
    # another process's registry does not contain it) is refused fail-closed.
    forged: DerivedHandle = {
        "handle_id": "deadbeef" * 4,
        "service_id": "outlook",
        "credential_mode": "oauth_user",
        "scopes": ("mail.read",),
        "generation": 1,
        "binding_fingerprint": "x" * 64,
        "issued_at": _T0,
        "not_after": _T0 + 10**9,
    }
    with pytest.raises(HandleNotIssuedError):
        ensure_usable(forged, now=_T0 + 1.0)


# --- Non-reversibility -----------------------------------------------------


def test_the_handle_does_not_carry_anything_that_reconstructs_the_binding() -> None:
    # Invariant 3. Feed identifying material through the binding, then prove none
    # of it appears anywhere in the handle's serialized form, and that the handle
    # exposes no field that would carry it.
    binding = _make_binding()
    handle = _make_handle(binding=binding)
    blob = repr(handle)

    assert binding["binding_id"] not in blob
    assert binding["subject_ref"] not in blob
    assert binding["tenant_ref"] not in blob
    assert binding["secret_ref"]["name"] not in blob

    forbidden_fields = {"binding_id", "subject_ref", "tenant_ref", "secret_ref"}
    assert forbidden_fields.isdisjoint(set(DerivedHandle.__annotations__))


# --- Negative --------------------------------------------------------------


def test_handle_id_is_random_not_derived_from_the_binding() -> None:
    binding = _make_binding()
    a = _make_handle(binding=binding)
    b = _make_handle(binding=binding)
    assert a["handle_id"] != b["handle_id"]
    for token in (binding["binding_id"], binding["subject_ref"], binding["tenant_ref"]):
        assert token not in a["handle_id"]
    assert len(a["handle_id"]) == 32
    int(a["handle_id"], 16)  # pure hex, raises if not


def test_handle_ids_do_not_repeat_across_many_derivations() -> None:
    binding = _make_binding()
    ids = {_make_handle(binding=binding)["handle_id"] for _ in range(200)}
    assert len(ids) == 200


def test_binding_fingerprint_is_one_way_and_matches_only_the_same_binding() -> None:
    binding = _make_binding()
    handle = _make_handle(binding=binding)
    fp = handle["binding_fingerprint"]
    assert fp != binding["binding_id"]
    assert binding["binding_id"] not in fp
    assert fp == _binding_fingerprint(binding["binding_id"])
    other = _make_binding()
    assert _binding_fingerprint(other["binding_id"]) != fp


def test_two_handles_from_one_binding_share_a_fingerprint_for_fencing() -> None:
    binding = _make_binding()
    a = _make_handle(binding=binding)
    b = _make_handle(binding=binding)
    assert a["binding_fingerprint"] == b["binding_fingerprint"]


def test_a_non_positive_ttl_is_refused() -> None:
    for bad_ttl in (0.0, -1.0):
        with pytest.raises(ValueError):
            _make_handle(ttl_seconds=bad_ttl)


def test_an_honest_handle_with_all_claims_matching_the_record_is_usable() -> None:
    # Belt-and-braces: an untouched handle whose scope/expiry/generation/
    # fingerprint all match the record passes cleanly.
    handle = _make_handle(requested=("mail.read", "mail.send"), now=_T0, ttl_seconds=300.0)
    ensure_usable(handle, now=_T0 + 10.0)  # no raise


def test_a_changed_binding_fingerprint_is_refused() -> None:
    handle = _make_handle(now=_T0, ttl_seconds=300.0)
    handle["binding_fingerprint"] = "0" * 64
    with pytest.raises(HandleTamperedError):
        ensure_usable(handle, now=_T0 + 1.0)


def test_handle_symbols_are_reachable_via_control_plane_not_the_top_level() -> None:
    # Canonical home is the control_plane subpackage; the top-level connections
    # package deliberately carries NO control-plane symbol (the second-spelling
    # alias was a rename trap L02 reverted for twice). Pin where a name lives and
    # where it must NOT be aliased.
    handle_names = (
        "DerivedHandle",
        "TrustedHandleView",
        "HandleScopeError",
        "HandleExpiredError",
        "HandleNotIssuedError",
        "HandleTamperedError",
        "derive_handle",
        "ensure_usable",
        "is_expired",
        "HANDLE_SCHEMA_VERSION",
    )
    for name in handle_names:
        assert name in cp.__all__, f"{name} missing from control_plane.__all__"
        assert hasattr(cp, name), f"{name} not reachable via control_plane"
        assert name not in connections.__all__, f"{name} leaked into connections.__all__"
        assert not hasattr(connections, name), f"{name} is a top-level connections alias"
