"""W01 · L09 executor tests.

These pin the properties the whole control-plane rests on: the four judgments run
BEFORE any transport call, a denied gate emits ZERO calls, routing reads the
TRUSTED handle view (never the mutable handle) AND the caller's own claims about
those axes must agree with it, time comes from a server-side clock re-read per
call and per page, a delete is replay-gated like any other effect, a paging walk
carries its original arguments on every page, a 412 is preserved as a structured
signal instead of flattened (and never fabricates a precondition), and pagination
advances for real.

The transport is an in-memory fake throughout -- that is the point of injecting
it. The one exception is the production-composition block at the bottom, which
proves the REAL secret custody wiring with an injected vault and an injected
sender, so it too opens no socket.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

import kiro_crew.connections.control_plane as cp
from kiro_crew.connections.control_plane import executor as executor_mod
from kiro_crew.connections.control_plane import production as production_mod
from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import (
    Binding,
    VerifiedIdentity,
    binding_secret_ref,
    create_binding,
)
from kiro_crew.connections.control_plane.executor import (
    EXECUTOR_SCHEMA_VERSION,
    PageWalk,
    PreconditionFailure,
    TransportResponse,
    advance_page,
    classify_error,
    execute,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import OperationDescriptor
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    BindingCustodyGate,
    HttpReply,
    HttpRequest,
    SecretResolutionError,
    build_production_transport,
    resolve_binding_secret,
)
from kiro_crew.connections.control_plane.result import OperationResult
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_FAILED_NOT_APPLIED,
    ATTEMPT_UNKNOWN,
    args_fingerprint,
    record_attempt,
)
from kiro_crew.secrets import SecretValue

_T0 = 1_000_000.0
_GRANTED = ("mail.read", "mail.send")


# --- a counting, in-memory fake transport (NOT a real business write) ---------
class FakeTransport:
    """Records every call so a test can assert the gate emitted zero of them.

    In-memory only -- it performs no network and no business write. It hands
    back whatever ``TransportResponse`` the test queued, and remembers the
    trusted axes it was called with so routing can be checked.
    """

    def __init__(self, response: TransportResponse | list[TransportResponse]):
        self._responses = response if isinstance(response, list) else [response]
        self._i = 0
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> TransportResponse:
        self.calls.append(kwargs)
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


def _verifier(*, claimed_subject, claimed_tenant, service_id) -> VerifiedIdentity:
    return {
        "subject_ref": f"subject://verified/{claimed_subject}",
        "tenant_ref": f"tenant://verified/{claimed_tenant}",
    }


def _binding(service_id="outlook") -> Binding:
    return create_binding(
        service_id=service_id,
        claimed_subject="alice",
        claimed_tenant="acme",
        credential_mode="oauth_user",
        verifier=_verifier,
        slug="outlook",
    )


def _handle(
    *, binding: Binding | None = None, requested=("mail.read",), ttl=300.0
) -> DerivedHandle:
    return derive_handle(
        binding or _binding(),
        granted_scopes=_GRANTED,
        requested_scopes=requested,
        now=_T0,
        ttl_seconds=ttl,
    )


#: Minted ONCE by the real constructor; the production-transport tests seed the
#: vault under its OWN per-binding scoped secret_ref name (never a slug name).
_DEFAULT_BINDING: Binding = _binding()


def _descriptor(effect="read", modes=("oauth_user",), service="outlook") -> OperationDescriptor:
    return {
        "operation_id": "outlook.messages.list",
        "service_id": service,
        "operation_kind": "list",
        "effect": effect,
        "credential_modes": tuple(modes),
    }


def _ok_response(next_cursor=None) -> TransportResponse:
    result: OperationResult = {"status": "ok", "next_cursor": next_cursor, "payload": None}
    return TransportResponse(http_status=200, result=result)


def _kw(**over):
    base = dict(
        now=_T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),  # all None == ungoverned == permit
        governance_scope="tools",
        governance_item="messages.list",
    )
    base.update(over)
    return base


# --- schema version -----------------------------------------------------------
def test_executor_has_a_schema_version() -> None:
    assert isinstance(EXECUTOR_SCHEMA_VERSION, int)
    assert EXECUTOR_SCHEMA_VERSION >= 1


# --- the happy path emits exactly one call ------------------------------------
def test_authorized_call_emits_exactly_one_transport_call() -> None:
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), _handle(), transport, **_kw())
    assert outcome.ok
    assert len(transport.calls) == 1


# --- THE judgment point: a denied gate emits ZERO transport calls -------------
def test_denied_credential_mode_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    # offered mode not permitted (permitted set is empty == deny-by-default)
    outcome = execute(
        _descriptor(),
        _handle(),
        transport,
        **_kw(permitted=declare_permitted_modes(())),
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0  # THE assertion: transport never touched


def test_undeclared_mode_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    # descriptor declares only service_to_service; caller offers oauth_user
    outcome = execute(
        _descriptor(modes=("service_to_service",)),
        _handle(),
        transport,
        **_kw(offered_mode="oauth_user", permitted=declare_permitted_modes(("oauth_user",))),
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0


def test_unknown_governance_scope_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    outcome = execute(
        _descriptor(),
        _handle(),
        transport,
        **_kw(governance_scope="definitely.not.a.catalog.scope", governance_item="x"),
    )
    assert outcome.error is not None
    assert len(transport.calls) == 0


def test_tampered_handle_scope_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    handle = _handle(requested=("mail.read",))
    tampered = dict(handle)
    tampered["scopes"] = ("mail.read", "mail.send")  # widened beyond issued
    outcome = execute(_descriptor(), tampered, transport, **_kw())
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert outcome.view is None  # handle rejected before a view resolved
    assert len(transport.calls) == 0


def test_expired_handle_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    handle = _handle(ttl=100.0)
    outcome = execute(_descriptor(), handle, transport, **_kw(now=_T0 + 200.0))
    assert outcome.error is not None
    assert len(transport.calls) == 0


def test_non_finite_now_emits_zero_calls() -> None:
    for bad in (math.nan, math.inf, -math.inf):
        t = FakeTransport(_ok_response())
        outcome = execute(_descriptor(), _handle(), t, **_kw(now=bad))
        assert outcome.error is not None
        assert outcome.error["error_class"] == "input"
        assert len(t.calls) == 0


def test_write_replay_refuse_on_unknown_emits_zero_calls() -> None:
    transport = FakeTransport(_ok_response())
    desc = _descriptor(effect="external_send")
    args = {"to": "x", "body": "y"}
    record = record_attempt(
        operation_id=desc["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="k1",
        outcome=ATTEMPT_UNKNOWN,
    )
    outcome = execute(
        desc,
        _handle(),
        transport,
        **_kw(),
        request_args=args,
        request_idempotency_key="k1",
        attempt_record=record,
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "conflict"
    assert len(transport.calls) == 0  # uncertain write is NOT blindly reissued


# --- routing trusts the VIEW, not the handle ----------------------------------
def test_routing_ignores_a_mutated_handle_service_id() -> None:
    binding = _binding(service_id="outlook")
    handle = _handle(binding=binding)
    # Mutate the handle's self-reported service_id to a DIFFERENT service.
    lying = dict(handle)
    lying["service_id"] = "github"
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), lying, transport, **_kw())
    # ensure_usable refuses a tampered service_id outright -> zero calls, so the
    # mutation cannot even reach routing. Either way, routing never uses "github".
    if outcome.ok:
        assert transport.calls[0]["service_id"] == "outlook"
    else:
        assert len(transport.calls) == 0
    assert all(c["service_id"] != "github" for c in transport.calls)


def test_routing_uses_the_trusted_view_service_id() -> None:
    binding, handle = _bound()
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.ok
    assert transport.calls[0]["service_id"] == "outlook"
    assert transport.calls[0]["credential_mode"] == "oauth_user"
    assert outcome.view is not None
    assert outcome.view.service_id == "outlook"


# --- HTTP 412: structured, not flattened --------------------------------------
def test_http_412_is_preserved_as_a_structured_signal() -> None:
    transport = FakeTransport(
        TransportResponse(
            http_status=412,
            preconditions=("If-Match",),
            etag='W/"v7"',
            detail="etag mismatch",
        )
    )
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    # Not flattened into a generic error: the structured signal is present.
    assert outcome.precondition is not None
    assert isinstance(outcome.precondition, PreconditionFailure)
    assert outcome.precondition.preconditions == ("If-Match",)
    assert outcome.precondition.server_etag == 'W/"v7"'
    # Maps to L07's failed_not_applied (write did not land).
    assert outcome.precondition.recorded_outcome == ATTEMPT_FAILED_NOT_APPLIED
    # A caller that wants the flat class can still read it, but it is a typed
    # conflict, not a swallowed error.
    assert outcome.precondition.error["error_class"] == "conflict"
    assert outcome.error is None  # NOT surfaced as a generic error


def test_412_recorded_outcome_is_the_replayable_branch() -> None:
    # failed_not_applied is exactly the branch replay_decision ALLOWS -- but the
    # executor still does not auto-reissue; it hands back the structured signal.
    assert ATTEMPT_FAILED_NOT_APPLIED in cp.ATTEMPT_OUTCOMES
    transport = FakeTransport(
        TransportResponse(http_status=412, preconditions=("If-Unmodified-Since",), etag=None)
    )
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    assert outcome.precondition.recorded_outcome == ATTEMPT_FAILED_NOT_APPLIED
    assert outcome.precondition.server_etag is None  # provider sent none; preserved as None
    # Exactly one call was emitted (the write was attempted, got 412).
    assert len(transport.calls) == 1


# --- error classification -----------------------------------------------------
def test_classify_error_maps_statuses() -> None:
    assert classify_error(_ok_response()) is None
    assert classify_error(TransportResponse(http_status=404))["error_class"] == "not_found"
    assert classify_error(TransportResponse(http_status=401))["error_class"] == "auth"
    assert classify_error(TransportResponse(http_status=403))["error_class"] == "forbidden"
    assert (
        classify_error(TransportResponse(http_status=429, retry_after_seconds=30))["error_class"]
        == "throttle"
    )
    assert classify_error(TransportResponse(http_status=503))["error_class"] == "temporary"
    # 412 is NOT classified as a flat error here -- it is a structured signal.
    # classify_error would call it conflict, but execute() never routes a 412
    # through classify_error (covered by the 412 tests above).


def test_transport_error_is_surfaced_as_typed_error() -> None:
    transport = FakeTransport(TransportResponse(http_status=404, detail="no such message"))
    outcome = execute(_descriptor(), _handle(), transport, **_kw())
    assert outcome.error is not None
    assert outcome.error["error_class"] == "not_found"
    assert len(transport.calls) == 1  # the call WAS emitted; provider said 404


# --- pagination: real advance, terminates, no drop/dup ------------------------
def test_pagination_walks_all_pages_and_terminates() -> None:
    transport = FakeTransport(
        [
            _ok_response(next_cursor="c1"),
            _ok_response(next_cursor="c2"),
            _ok_response(next_cursor=None),
        ]
    )
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        clock=lambda: _T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    pages = []
    while not walk.done:
        pages.append(advance_page(walk))
    assert walk.pages == 3
    assert len(pages) == 3
    assert walk.done
    # Cursors advanced in order: no page dropped, none duplicated.
    seen_cursors = [c["request_args"]["cursor"] for c in transport.calls]
    assert seen_cursors == [None, "c1", "c2"]


def test_pagination_refuses_to_loop_on_a_repeated_cursor() -> None:
    # A provider that keeps returning the SAME cursor must not spin forever.
    transport = FakeTransport(_ok_response(next_cursor="stuck"))
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        clock=lambda: _T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    outcomes = []
    for _ in range(10):  # bounded: the walk must terminate well before this
        if walk.done:
            break
        outcomes.append(advance_page(walk))
    assert walk.done
    assert outcomes[-1].error is not None
    assert outcomes[-1].error["error_class"] == "temporary"


def test_pagination_stops_on_a_denied_gate_without_emitting() -> None:
    transport = FakeTransport(_ok_response(next_cursor="c1"))
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        clock=lambda: _T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(()),  # deny-by-default
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    outcome = advance_page(walk)
    assert outcome.error is not None
    assert walk.done
    assert walk.pages == 0
    assert len(transport.calls) == 0


# --- guard: exports live only on control_plane, NOT top-level connections -----
def test_executor_symbols_are_reachable_on_control_plane() -> None:
    for name in (
        "EXECUTOR_SCHEMA_VERSION",
        "execute",
        "advance_page",
        "classify_error",
        "ExecutionOutcome",
        "PageWalk",
        "PreconditionFailure",
        "Transport",
        "TransportResponse",
    ):
        assert hasattr(cp, name), name
        assert name in cp.__all__, name


def test_executor_symbols_are_not_top_level_connections_reexports() -> None:
    import kiro_crew.connections as c

    for name in (
        "EXECUTOR_SCHEMA_VERSION",
        "execute",
        "advance_page",
        "classify_error",
        "ExecutionOutcome",
        "PageWalk",
        "PreconditionFailure",
        "TransportResponse",
        "build_production_transport",
        "resolve_binding_secret",
        "urllib_http_send",
    ):
        assert not hasattr(c, name), f"{name} leaked to top-level connections"


# =============================================================================
# Regressions: the caller's claims must AGREE with the trusted view (defect 1)
# =============================================================================
def test_offered_mode_disagreeing_with_the_view_emits_zero_calls() -> None:
    # The handle was issued for oauth_user. The descriptor declares BOTH modes,
    # so permit_operation alone would ALLOW an offered service_to_service -- it
    # never sees the handle. The call would then be emitted under the handle's
    # oauth_user credential: validated one mode, used another.
    transport = FakeTransport(_ok_response())
    outcome = execute(
        _descriptor(modes=("oauth_user", "service_to_service")),
        _handle(),
        transport,
        **_kw(
            offered_mode="service_to_service",
            permitted=declare_permitted_modes(("oauth_user", "service_to_service")),
        ),
    )
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0  # THE assertion: nothing was emitted
    # The view still resolved, so an audit can see which mode was actually issued.
    assert outcome.view is not None
    assert outcome.view.credential_mode == "oauth_user"


def test_descriptor_service_disagreeing_with_the_view_emits_zero_calls() -> None:
    # A handle for outlook must not authorize a github operation, even though the
    # executor would have routed the emit at the trusted outlook service: the
    # operation being authorized and the service being reached must be the same.
    transport = FakeTransport(_ok_response())
    outcome = execute(
        _descriptor(service="github"),
        _handle(),
        transport,
        **_kw(),
    )
    assert not outcome.ok
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    assert len(transport.calls) == 0
    assert outcome.view is not None
    assert outcome.view.service_id == "outlook"


def test_agreeing_mode_and_service_still_pass() -> None:
    # The cross-check must not deny the ordinary case.
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), _handle(), transport, **_kw())
    assert outcome.ok
    assert len(transport.calls) == 1


# =============================================================================
# Regressions: time comes from a SERVER-SIDE clock, re-read per call (defect 2)
# =============================================================================
class Ticker:
    """A deterministic clock: hands back each queued instant, then holds the last."""

    def __init__(self, *instants: float):
        self._instants = list(instants)
        self.reads = 0

    def __call__(self) -> float:
        instant = self._instants[min(self.reads, len(self._instants) - 1)]
        self.reads += 1
        return instant


def _kw_no_now(**over):
    kw = _kw(**over)
    kw.pop("now", None)
    return kw


def test_execute_reads_the_clock_when_no_now_is_pinned() -> None:
    clock = Ticker(_T0)
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), _handle(), transport, **_kw_no_now(clock=clock))
    assert outcome.ok
    assert clock.reads == 1  # the instant came from the clock, not the caller


def test_execute_clock_reading_decides_expiry_not_the_callers_word() -> None:
    # A caller-asserted PAST instant is perfectly finite, so the finiteness check
    # cannot catch it. The clock is what says the handle is expired.
    handle = _handle(ttl=100.0)
    transport = FakeTransport(_ok_response())
    outcome = execute(_descriptor(), handle, transport, **_kw_no_now(clock=Ticker(_T0 + 500.0)))
    assert outcome.error is not None
    assert outcome.error["error_class"] == "input"  # expired
    assert len(transport.calls) == 0


def test_pagewalk_holds_a_clock_not_a_frozen_now() -> None:
    import dataclasses

    fields = {f.name for f in dataclasses.fields(PageWalk)}
    assert "clock" in fields
    assert "now" not in fields, "a cached instant makes every page reuse page 1's expiry"


def test_pagewalk_rejudges_expiry_on_every_page() -> None:
    # The handle lives 100s. Page 1 at T0 is fine; by page 2 the clock has moved
    # past the expiry, so the walk STOPS instead of running the rest of a long
    # pagination on the judgment made at page 1.
    clock = Ticker(_T0, _T0 + 500.0)
    transport = FakeTransport([_ok_response(next_cursor="c1"), _ok_response(next_cursor=None)])
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(ttl=100.0),
        transport=transport,
        clock=clock,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
    )
    first = advance_page(walk)
    assert first.ok
    assert walk.pages == 1
    second = advance_page(walk)
    assert second.error is not None
    assert second.error["error_class"] == "input"  # expired between pages
    assert walk.done
    assert walk.pages == 1
    assert len(transport.calls) == 1  # page 2 never reached the transport
    assert clock.reads == 2  # one fresh reading PER page


# =============================================================================
# Regression: delete is replay-gated like any other effect (defect 3)
# =============================================================================
def test_delete_is_in_the_replay_gated_effect_set() -> None:
    assert "delete" in executor_mod._NON_IDEMPOTENT_EFFECTS
    # read is the only ungated effect: it applies no effect to replay.
    assert executor_mod._NON_IDEMPOTENT_EFFECTS == frozenset(
        {"write", "delete", "share", "external_send", "admin", "billable"}
    )


def test_delete_with_an_unknown_prior_attempt_is_refused_and_emits_zero_calls() -> None:
    # L07 refuses to call a delete idempotent: a second DELETE can land on a
    # RECREATED resource. Excluding delete from the gate meant replay_decision
    # never ran, so that refusal could not happen here.
    transport = FakeTransport(_ok_response())
    desc = _descriptor(effect="delete")
    args = {"message_id": "m1"}
    record = record_attempt(
        operation_id=desc["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="k1",
        outcome=ATTEMPT_UNKNOWN,
    )
    outcome = execute(
        desc,
        _handle(),
        transport,
        **_kw(),
        request_args=args,
        request_idempotency_key="k1",
        attempt_record=record,
    )
    assert outcome.error is not None
    assert outcome.error["error_class"] == "conflict"
    assert len(transport.calls) == 0


def test_delete_asserted_idempotent_by_the_caller_is_still_allowed() -> None:
    # Gating delete does not forbid it: L07's explicit idempotency assertion is
    # what allows the replay, which is exactly what gating makes reachable.
    transport = FakeTransport(_ok_response())
    desc = _descriptor(effect="delete")
    args = {"message_id": "m1"}
    record = record_attempt(
        operation_id=desc["operation_id"],
        args_fingerprint=args_fingerprint(args),
        idempotency_key="k1",
        outcome=ATTEMPT_UNKNOWN,
        idempotent=True,
    )
    outcome = execute(
        desc,
        _handle(),
        transport,
        **_kw(),
        request_args=args,
        request_idempotency_key="k1",
        attempt_record=record,
    )
    assert outcome.ok
    assert len(transport.calls) == 1


# =============================================================================
# Regression: a paging walk carries its BASE args on every page (defect 4)
# =============================================================================
def test_pagewalk_carries_base_args_on_every_page() -> None:
    transport = FakeTransport(
        [
            _ok_response(next_cursor="c1"),
            _ok_response(next_cursor="c2"),
            _ok_response(next_cursor=None),
        ]
    )
    base = {"folder": "Inbox", "unread_only": True}
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        clock=lambda: _T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
        base_args=base,
    )
    while not walk.done:
        advance_page(walk)
    assert walk.pages == 3
    sent = [c["request_args"] for c in transport.calls]
    assert sent == [
        {"folder": "Inbox", "unread_only": True, "cursor": None},
        {"folder": "Inbox", "unread_only": True, "cursor": "c1"},
        {"folder": "Inbox", "unread_only": True, "cursor": "c2"},
    ]
    # Page 2+ fingerprints as the SAME query as page 1 modulo its cursor, which
    # is what L07 attribution compares. Dropping the filters changed the query.
    assert all(args["folder"] == "Inbox" for args in sent)


def test_pagewalk_base_args_are_not_mutated_by_the_walk() -> None:
    transport = FakeTransport([_ok_response(next_cursor="c1"), _ok_response(next_cursor=None)])
    base = {"folder": "Inbox"}
    walk = PageWalk(
        descriptor=_descriptor(),
        handle=_handle(),
        transport=transport,
        clock=lambda: _T0,
        offered_mode="oauth_user",
        permitted=declare_permitted_modes(("oauth_user",)),
        layers=LayerCeilings(),
        governance_scope="tools",
        governance_item="messages.list",
        base_args=base,
    )
    while not walk.done:
        advance_page(walk)
    assert base == {"folder": "Inbox"}  # no cursor leaked into the caller's dict


# =============================================================================
# Regression: a 412 never fabricates a precondition (defect 5)
# =============================================================================
def test_412_with_no_reported_precondition_reports_condition_unknown() -> None:
    transport = FakeTransport(TransportResponse(http_status=412, preconditions=(), etag=None))
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    assert outcome.precondition is not None
    # Empty stays empty: no If-Match invented on the provider's behalf.
    assert outcome.precondition.preconditions == ()
    assert outcome.precondition.condition_unknown is True
    assert outcome.precondition.server_etag is None
    assert "If-Match" not in outcome.precondition.error["detail"]
    # The caller is told to read back, since nothing here says what failed.
    assert "read the resource back" in outcome.precondition.error["detail"]
    assert outcome.precondition.recorded_outcome == ATTEMPT_FAILED_NOT_APPLIED


def test_412_with_a_reported_precondition_is_not_condition_unknown() -> None:
    transport = FakeTransport(
        TransportResponse(http_status=412, preconditions=("If-Match",), etag='W/"v9"')
    )
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    assert outcome.precondition is not None
    assert outcome.precondition.preconditions == ("If-Match",)
    assert outcome.precondition.condition_unknown is False
    assert outcome.precondition.server_etag == 'W/"v9"'


def test_412_with_an_etag_but_no_precondition_name_is_not_unknown() -> None:
    # An ETag IS something to re-derive against, so the condition is not unknown
    # -- but the name is still not fabricated.
    transport = FakeTransport(TransportResponse(http_status=412, preconditions=(), etag='W/"v9"'))
    outcome = execute(
        _descriptor(effect="write"), _handle(requested=("mail.send",)), transport, **_kw()
    )
    assert outcome.precondition is not None
    assert outcome.precondition.preconditions == ()
    assert outcome.precondition.condition_unknown is False
    assert outcome.precondition.server_etag == 'W/"v9"'
    assert "If-Match" not in outcome.precondition.error["detail"]


# =============================================================================
# Regression: the PRODUCTION composition is real (defect 6)
# =============================================================================
class StubVault:
    """A stand-in for the vault's READ side (``SecretVault.get``).

    Injected so this test needs no encrypted store on disk. It records the names
    it was asked for, which is what proves the transport resolves the BINDING's
    recorded ``secret_ref`` name rather than some name of its own.
    """

    def __init__(self, entries: dict[str, str]):
        self._entries = entries
        self.asked: list[str] = []

    def get(self, name: str):
        self.asked.append(name)
        raw = self._entries.get(name)
        return None if raw is None else SecretValue(raw)


def _locator(**kwargs) -> HttpRequest:
    """A stand-in for the VENDOR-owned request shaper."""

    return HttpRequest(
        method="GET",
        url="https://graph.example.invalid/v1.0/me/messages",
        headers={"Accept": "application/json"},
    )


def _gate_for(binding: Binding, handle: DerivedHandle) -> BindingCustodyGate:
    """The custody gate a transport is composed with FOR ``handle``'s binding.

    Built from the TRUSTED view, which is the only place the binding fingerprint
    can honestly come from: the transport must be composed for the same binding
    identity that ``ensure_usable`` will hand it at call time, or it refuses.
    """

    view = ensure_usable(handle, now=_T0)
    return BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)


def _live_store(root: Path, *bindings: Binding) -> BindingStore:
    """A REAL on-disk BindingStore holding ``bindings``, for the live-store fence."""

    store = BindingStore(root / "connections" / "control_plane_bindings.json")
    for index, binding in enumerate(bindings):
        store.insert(
            binding,
            deployment_id=f"deployment://test/graph/{index}",
            kiro_principal="kiro://test/owner",
        )
    return store


def _bound(**kw) -> tuple[Binding, DerivedHandle]:
    """A binding plus a handle derived from it (the binding is what gets fenced).

    Uses the shared ``_DEFAULT_BINDING`` unless a `binding` is passed, so its
    per-binding scoped secret_ref (the constructor's own record) is what the tests
    seed -- the per-binding path, not a slug path.
    """

    binding = kw.pop("binding", None) or dict(_DEFAULT_BINDING)
    return binding, _handle(binding=binding, **kw)


def test_the_module_no_longer_claims_it_performs_no_network() -> None:
    # The claim was false once a real transport existed, and a delivery that
    # misdescribes itself is the defect, not the docstring wording.
    doc = executor_mod.__doc__ or ""
    assert "no real network" not in doc
    assert "no network" not in doc
    # And it points at where the network actually happens.
    assert "production" in doc


def test_production_transport_resolves_the_secret_through_the_vault(tmp_path: Path) -> None:
    secret_ref = _DEFAULT_BINDING["secret_ref"]
    vault = StubVault({secret_ref["name"]: "tok-live"})
    sent: list[HttpRequest] = []

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        sent.append(request)
        return HttpReply(status=200, headers={}, body=b"{}")

    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=vault,
        locator=_locator,
        http_send=_send,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.ok
    # The vault was asked for the BINDING's recorded PER-BINDING entry name (the
    # scoped name the constructor produced), not a slug name.
    assert vault.asked == [_DEFAULT_BINDING["secret_ref"]["name"]]
    # The resolved secret reached the wire as a bearer credential, and the
    # vendor locator never had to see it.
    assert sent[0].headers["Authorization"] == "Bearer tok-live"
    assert "Authorization" not in _locator().headers


def test_production_transport_uses_the_existing_secret_vault_mechanism() -> None:
    # Not a new vault: the real SecretVault satisfies the injection protocol the
    # composition declares, and SecretValue is the existing opaque wrapper.
    from kiro_crew.secrets import SecretVault

    assert hasattr(SecretVault, "get")
    assert isinstance(SecretValue("x"), SecretValue)
    assert repr(SecretValue("x")) == "SecretValue(****)"
    ref = binding_secret_ref("outlook")
    assert resolve_binding_secret(ref, vault=StubVault({ref["name"]: "v"})).reveal() == "v"


def test_production_transport_missing_secret_is_a_typed_auth_failure(tmp_path: Path) -> None:
    secret_ref = binding_secret_ref("outlook")
    vault = StubVault({})  # nothing stored

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        raise AssertionError("must not send without a credential")

    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=vault,
        locator=_locator,
        http_send=_send,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())
    assert outcome.error is not None
    assert outcome.error["error_class"] == "auth"
    # The vault entry name is not echoed into the caller-visible detail.
    assert secret_ref["name"] not in outcome.error["detail"]


def test_resolve_binding_secret_refuses_a_foreign_backend() -> None:
    ref = dict(binding_secret_ref("outlook"))
    ref["backend"] = "somewhere-else"
    with pytest.raises(SecretResolutionError):
        resolve_binding_secret(ref, vault=StubVault({ref["name"]: "v"}))


def test_production_transport_maps_a_412_to_the_preconditions_it_asserted(tmp_path: Path) -> None:
    secret_ref = _DEFAULT_BINDING["secret_ref"]
    vault = StubVault({secret_ref["name"]: "tok"})

    def _if_match_locator(**kwargs) -> HttpRequest:
        return HttpRequest(
            method="PATCH",
            url="https://graph.example.invalid/v1.0/me/messages/m1",
            headers={"If-Match": 'W/"v1"'},
            body=b"{}",
        )

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        return HttpReply(status=412, headers={"ETag": 'W/"v2"'}, body=b"")

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=vault,
        locator=_if_match_locator,
        http_send=_send,
    )
    outcome = execute(_descriptor(effect="write"), handle, transport, **_kw())
    assert outcome.precondition is not None
    # Reported because the REQUEST sent it -- not because 412 defaults to it.
    assert outcome.precondition.preconditions == ("If-Match",)
    assert outcome.precondition.server_etag == 'W/"v2"'
    assert outcome.precondition.condition_unknown is False


def test_production_transport_412_without_an_asserted_precondition_is_unknown(
    tmp_path: Path,
) -> None:
    secret_ref = _DEFAULT_BINDING["secret_ref"]
    vault = StubVault({secret_ref["name"]: "tok"})

    def _send(request: HttpRequest, *, timeout_seconds: float) -> HttpReply:
        return HttpReply(status=412, headers={}, body=b"")

    binding, handle = _bound(requested=("mail.send",))
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=vault,
        locator=_locator,
        http_send=_send,
    )
    outcome = execute(_descriptor(effect="write"), handle, transport, **_kw())
    assert outcome.precondition is not None
    assert outcome.precondition.preconditions == ()
    assert outcome.precondition.condition_unknown is True


def test_production_transport_refuses_a_non_https_url_without_sending(tmp_path: Path) -> None:
    secret_ref = _DEFAULT_BINDING["secret_ref"]
    vault = StubVault({secret_ref["name"]: "tok"})

    def _plain_http_locator(**kwargs) -> HttpRequest:
        return HttpRequest(method="GET", url="http://graph.example.invalid/v1.0/me")

    binding, handle = _bound()
    transport = build_production_transport(
        gate=_gate_for(binding, handle),
        store=_live_store(tmp_path, binding),
        vault=vault,
        locator=_plain_http_locator,
    )
    outcome = execute(_descriptor(), handle, transport, **_kw())
    # urllib_http_send refuses before opening a socket; the executor sees input.
    assert outcome.error is not None
    assert outcome.error["error_class"] == "input"


def test_production_module_performs_no_network_at_import_time() -> None:
    # A composition that dialled out on import could not be imported by a test
    # suite at all. Pinned structurally: the module exposes only factories, and
    # importing it (already done at the top of this file) constructed no client.
    assert production_mod.PRODUCTION_SCHEMA_VERSION >= 1
    assert callable(production_mod.build_production_transport)
    assert callable(production_mod.urllib_http_send)
    assert production_mod.neutral_decode(HttpReply(status=200)) == {
        "status": "ok",
        "next_cursor": None,
        # An empty 2xx body genuinely returned no data, so the payload channel is
        # empty -- not merely dropped (see the data-channel suite for the rows
        # that DO carry bytes).
        "payload": None,
    }


def test_production_symbols_are_reachable_on_control_plane_only() -> None:
    import kiro_crew.connections as connections

    for name in (
        "PRODUCTION_SCHEMA_VERSION",
        "HttpReply",
        "HttpRequest",
        "SecretResolutionError",
        "SecretStore",
        "build_production_transport",
        "resolve_binding_secret",
        "urllib_http_send",
    ):
        assert hasattr(cp, name), name
        assert name in cp.__all__, name
        assert name not in connections.__all__, f"{name} leaked into connections.__all__"


def test_every_authorization_decision_emits_one_sel_event(monkeypatch) -> None:
    """F1: allow and deny each leave exactly one SEL audit event.

    ``execute`` reaches one final authorization decision per call. Before this
    fix neither allow nor deny recorded anything, so an authorization left no
    audit trail. Capture ``sel().log`` and assert: a denied gate emits ONE deny
    event (carrying the redacted error_class, no secret), and a permitted call
    emits ONE allow event -- both under the connections authorization event type.
    """

    import kiro_crew.sel as sel_mod  # noqa: F401  (kept for symmetry; patch target below)

    logged: list = []

    class _CapturingSel:
        def log(self, event) -> None:
            logged.append(event)

    # ``executor`` binds ``sel`` at module import (top-level-imports), so the
    # capture must replace the name in the executor module, not in ``kiro_crew.sel``.
    monkeypatch.setattr(executor_mod, "sel", lambda: _CapturingSel())

    # Deny: offered mode not permitted -> one deny event, zero transport calls.
    transport = FakeTransport(_ok_response())
    denied = execute(
        _descriptor(), _handle(), transport, **_kw(permitted=declare_permitted_modes(()))
    )
    assert denied.error is not None
    assert len(transport.calls) == 0
    deny_events = [e for e in logged if e.event_type == "connections.operation.authorization"]
    assert len(deny_events) == 1
    assert deny_events[0].outcome == "deny"
    # The redacted error_class rides the deny; the secret value never does.
    assert "error_class=auth" in deny_events[0].resources

    # Allow: a permitted call dispatches -> one allow event.
    logged.clear()
    allowed = execute(_descriptor(), _handle(), FakeTransport(_ok_response()), **_kw())
    assert allowed.error is None
    allow_events = [e for e in logged if e.event_type == "connections.operation.authorization"]
    assert len(allow_events) == 1
    assert allow_events[0].outcome == "allow"

    # F3: the gate authorized, but the TRANSPORT's custody / live-store fence then
    # refuses (a revoked binding -> HTTP 401 auth). The audit must record this as
    # DENY, not the pre-fix "allow before the fence" -- the event must match what
    # actually happened.
    logged.clear()
    refusing = FakeTransport(TransportResponse(http_status=401, detail="binding not live"))
    fenced = execute(_descriptor(), _handle(), refusing, **_kw())
    assert fenced.error is not None and fenced.error["error_class"] == "auth"
    fence_events = [e for e in logged if e.event_type == "connections.operation.authorization"]
    assert len(fence_events) == 1
    assert fence_events[0].outcome == "deny"  # NOT allow -- the fence refused
    assert "error_class=auth" in fence_events[0].resources
