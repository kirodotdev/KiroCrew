"""W01 · L09: the executor that wires the four control-plane judgments together.

L01..L08 delivered four decision primitives, but nothing yet CALLS them in the
one order a real dispatch must. This module is that caller. It is the seam every
provider stream (W02 GitHub, W05 Graph, ...) routes an operation through, and it
enforces -- BEFORE any transport call is emitted -- the full gate chain:

    1. trusted handle view   :func:`~...control_plane.handle.ensure_usable`
    2. credential-mode permit :func:`~...control_plane.auth_modes.permit_operation`
    3. governance intersection :func:`~...control_plane.policy.decide`
    4. write-replay gate       :func:`~...control_plane.writes.replay_decision`

Any one of these rejecting returns a typed :class:`OperationError` and the
transport is NEVER touched -- "emit first, decide after" would make all four
upstream slices dead code, so the order is load-bearing and tested by asserting
the fake transport's call count stays at zero on every deny path.

**Routing trusts the view, never the handle.** ``service_id`` and
``credential_mode`` are read ONLY off the :class:`TrustedHandleView` that
:func:`ensure_usable` returns from its issuance record -- never off the mutable
handle dict. Reading them off the handle would be the "validate then use the
unvalidated value" hole L08 exists to close; a test proves that mutating the
handle's ``service_id`` does not change where the executor routes.

Resolving the trusted view is not enough on its own, though: the CALLER also
hands in an ``offered_mode`` and a ``descriptor``, and both make claims about the
same two axes the view already decides. So before any permit decision runs, the
executor CROSS-CHECKS them against the view (:func:`_authorize` step 1b) and
refuses an ``auth`` error when either disagrees. Without that check a handle
issued for ``oauth_user`` / ``outlook`` would let a caller offer
``service_to_service`` (permitted purely because the descriptor happens to
declare both modes) or present a descriptor naming ``github`` -- validating one
value and then authorizing a different one, which is the same hole one layer up.

**Server clock, not the caller's word.** Time is derived from a SERVER-SIDE
clock, not from whatever instant the caller asserts: :func:`execute` and
:class:`PageWalk` take a ``clock`` callable (defaulting to :func:`time.time`) and
call it FRESH on every call and on every page, so expiry is re-judged against the
real current instant each time rather than once at the top of a walk. A finiteness
check stays in front of every time-sensitive judgment (:func:`ensure_usable`
refuses a ``NaN`` / ``inf`` clock and L08 turned a car over on exactly that), but
finiteness alone never made a caller-supplied instant trustworthy -- a PAST
timestamp is perfectly finite and would keep an expired handle alive for the
length of a paging walk. The explicit ``now`` argument survives ONLY as a
deterministic-test override; leave it unset and the clock decides.

**HTTP 412 is preserved, not flattened.** A precondition-failed response
(``If-Match`` / ``If-Unmodified-Since`` ETag mismatch) is NOT collapsed into a
generic ``conflict``: the executor returns a structured
:class:`PreconditionFailure` carrying the failed preconditions and the server's
current ETag, so the CALLER can decide to re-read and re-derive rather than blind
retry. It maps the write's recorded outcome to L07's ``failed_not_applied`` (the
write provably did not land), which is the branch ``replay_decision`` *allows* --
but "allowed to replay" is NOT "safe to replay as-is": a 412 tells you only that
the precondition you asserted is false, never the server's current state. The
correct shape is 412 -> preserve the structured signal -> readback + re-derive
the precondition -> only then retry. The executor deliberately does NOT
auto-reissue; it hands the structured signal back and stops.

When the transport reports NO failed precondition and no ETag, the executor does
NOT fabricate one: it reports ``preconditions == ()`` and sets
``condition_unknown`` (see :class:`PreconditionFailure`). Naming ``If-Match`` on
a response that never mentioned it would invent readback semantics that belong to
the vendor owner, and would tell the caller to re-derive a condition it may never
have asserted.

**A success carries DATA, not just a verdict.** The gate chain is the point of
this module, but a chain that authorizes perfectly and then returns nothing is
not a dispatch. So the 2xx envelope carries a
:data:`~kiro_crew.connections.control_plane.result.OperationPayload` -- a
collection of items, a single object, or raw bytes -- and
:attr:`ExecutionOutcome.payload` is where a consumer reads it. The executor does
not INTERPRET that payload: the injected
:data:`~kiro_crew.connections.control_plane.production.ResultDecode` produces it
(the neutral default carries the body's raw bytes verbatim; a vendor decode turns
them into items/object/cursor), and this module only carries it through unchanged.

**ONE cursor.** Pagination advances on
:attr:`~kiro_crew.connections.control_plane.result.OperationResult.next_cursor`
and on nothing else -- :class:`PageWalk` reads it there, and the collection
payload does not carry a copy. A second copy is a second thing that can be wrong,
and a disagreement between them shows up as a walk that stops while pages remain
or refetches one it already has, neither of which is distinguishable from a
correct result at this seam.

**Response metadata is ALLOWLISTED.** The rate-limit family reaches a caller
through :attr:`ExecutionOutcome.metadata` (see :data:`ResponseMetadata`) and
through nothing else. A response header set is a credential surface --
``Set-Cookie`` mints a session, ``WWW-Authenticate`` carries challenge material --
so the transport copies only the names on a closed allowlist and drops the rest,
which is the response-side counterpart of the redirect credential strip.

**Boundaries.** Decision + dispatch glue. The transport is an injected callable
-- an in-memory fake in every unit test, and the REAL composition (vault-resolved
secret custody plus a stdlib HTTP client) in
:mod:`kiro_crew.connections.control_plane.production`, which is what makes this
an executor rather than a decision table. MS's own 412 / readback /
baseline / fresh / locator revalidation semantics live under
``vendors/microsoft/**`` and are that owner's -- this module only preserves the
shared structured signal MS consumes; it changes nothing there. It also does not
claim to cover document- / provider-level query ACL: L06's five-layer
intersection and the handle scope narrowing do NOT substitute for that
(a separately-owned gap), and this module makes no such claim.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Tuple

from kiro_crew.connections.control_plane.auth_modes import (
    PermittedModes,
    permit_operation,
)
from kiro_crew.connections.control_plane.errors import (
    ErrorClass,
    OperationError,
    operation_error,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    HandleExpiredError,
    HandleNotIssuedError,
    HandleScopeError,
    HandleTamperedError,
    TrustedHandleView,
    ensure_usable,
)
from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
)
from kiro_crew.connections.control_plane.policy import LayerCeilings, decide
from kiro_crew.connections.control_plane.result import OperationPayload, OperationResult
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_FAILED_NOT_APPLIED,
    REPLAY_REFUSE,
    REPLAY_REUSE,
    AttemptRecord,
    ReplayDecision,
    replay_decision,
)
from kiro_crew.sel import SecurityEvent, sel

#: Bumped when this module's OUTER interface shape changes -- the executor
#: request/response envelope, the transport-callable contract, or the paging /
#: error-classification surface. Two downstreams (W02 GitHub, W05 Graph) encode
#: against these shapes, so they pin this number; a shape change that would make
#: an old pin decode wrong MUST bump it.
#:
#: ``2`` added two shape changes that an old pin WOULD decode wrong, so both had
#: to bump it rather than ride along on ``1``:
#:
#: * :class:`TransportResponse` and :class:`ExecutionOutcome` grew
#:   ``write_outcome`` -- the transport's way of saying "the effect of this
#:   non-idempotent write is UNKNOWN", which a consumer must record instead of
#:   L07's ``failed_not_applied`` or it hands ``replay_decision`` a determinate
#:   answer the wire never gave and gets a DOUBLE WRITE waved through;
#: * :data:`Transport` now also receives ``trusted_view`` -- the
#:   :class:`TrustedHandleView` itself -- so a transport can bind its credential
#:   custody to the identity the call was actually authorized for instead of one
#:   fixed at composition time (see
#:   :class:`~kiro_crew.connections.control_plane.production.BindingCustodyGate`).
#:
#: ``3``: the result envelope now carries DATA. :class:`OperationResult` grew a
#: required ``payload`` (see :data:`~kiro_crew.connections.control_plane.result.RESULT_SCHEMA_VERSION`
#: ``2``) and :class:`ExecutionOutcome` exposes it as :attr:`ExecutionOutcome.payload`.
#: An old pin would decode this wrong in the way that matters most: it would read
#: a success as carrying status + cursor and nothing else -- so a consumer written
#: against ``2`` drops
#: every item, object and byte the operation returned rather than failing loudly.
#:
#: ``4`` is two more outer-shape changes:
#:
#: * :class:`TransportResponse` and :class:`ExecutionOutcome` grew ``metadata``
#:   -- the ONE allowlisted surface response metadata (the rate-limit family)
#:   reaches a caller through. A ``3`` pin has no field for it, so a caller that
#:   needs to back off keeps reading the header set it should never have been
#:   handed, or nothing at all;
#: * the result envelope's cursor is now SINGLE-SOURCED:
#:   ``CollectionPayload.next_cursor`` is gone and
#:   :data:`~kiro_crew.connections.control_plane.result.RESULT_SCHEMA_VERSION` is
#:   ``3``. A ``3``-era producer that set the cursor only on the collection now
#:   builds an envelope with ``next_cursor=None``, which stops a
#:   :class:`PageWalk` after page one and reports it as a complete result -- a
#:   silent drop, so the number has to move for it.
EXECUTOR_SCHEMA_VERSION = 4

# --- effects that are non-idempotent by default (the write-replay gate runs) --
#: Effects whose ``unknown``-outcome replay must be gated by L07. Read from the
#: descriptor's ``effect`` -- never inferred from an operation's name.
#:
#: ``delete`` IS in this set. L07 deliberately refuses to treat a delete as
#: idempotent (see ``writes._is_replay_safe_after_unknown``): a second DELETE can
#: land on a resource that was RECREATED in the interim -- deleting someone
#: else's new resource -- and a given API's delete may not be idempotent at all.
#: Excluding it here would have re-opened exactly the hole L07 closed, one layer
#: up: the gate would never run, so the explicit ``idempotent=True`` assertion
#: L07 requires could not be consulted. ``read`` is the only effect left ungated,
#: because it applies no effect to replay.
_NON_IDEMPOTENT_EFFECTS = frozenset(
    {"write", "delete", "share", "external_send", "admin", "billable"}
)


def is_non_idempotent_effect(effect: str) -> bool:
    """True when ``effect`` is one whose replay L07 must gate.

    The ONE reading of :data:`_NON_IDEMPOTENT_EFFECTS`, made public because a
    second reader exists: the production transport has to decide, on an
    ambiguous mid-flight failure, whether the outcome is merely a failure or an
    UNKNOWN that must not be blind-replayed -- and that turns on the same
    question :func:`_authorize` step 4 asks. Two copies of this set would drift,
    and the copy that drifted low would silently ungate an effect.
    """

    return effect in _NON_IDEMPOTENT_EFFECTS


# --- the ONE controlled surface response metadata reaches a caller through -----
#: Allowlisted response metadata, as a read-only mapping with LOWERCASE keys.
#:
#: The rate-limit family is the reason this exists: a caller that has to back off
#: needs ``retry-after`` / ``x-ratelimit-remaining``, and today the only way to get
#: at a response header is to have the transport hand the whole header set over.
#: That is not acceptable, because a response header set is a CREDENTIAL SURFACE:
#: ``Set-Cookie`` mints a session, ``WWW-Authenticate`` / ``Proxy-Authenticate``
#: carry challenge material, and an echoed ``Authorization`` is the bearer token
#: itself. Handing them to a caller (which logs, serializes and forwards its
#: results) is the same defect as forwarding a credential across a redirect hop,
#: on the response side.
#:
#: So this is a CLOSED ALLOWLIST, not a denylist: the transport copies only the
#: names on
#: :data:`~kiro_crew.connections.control_plane.production.RESPONSE_METADATA_ALLOWLIST`
#: and DROPS everything else, so a header nobody has thought about yet -- a
#: provider's new session cookie spelling included -- is absent by default rather
#: than present until someone notices. A denylist would have the opposite failure
#: direction.
#:
#: Keys are canonicalized to lowercase (HTTP header names are case-insensitive, so
#: a caller must not have to guess ``Retry-After`` vs ``retry-after``). The mapping
#: is read-only: a caller cannot edit one call's metadata and have it seen
#: elsewhere.
ResponseMetadata = Mapping[str, str]

#: The metadata of a call that produced no response at all -- a denied gate, a
#: reused prior result, a failure before a socket existed. Empty and immutable, so
#: it is safe as a shared default on a frozen dataclass.
EMPTY_RESPONSE_METADATA: ResponseMetadata = MappingProxyType({})


# --- the structured transport outcome the injected transport returns ----------
@dataclass(frozen=True)
class TransportResponse:
    """What the injected transport hands back for ONE emitted call.

    The transport is the only thing that touches a network (a real HTTP client
    in production, an in-memory fake in tests). It returns this structured
    envelope rather than raising, so the executor classifies uniformly.

    ``http_status`` -- the HTTP status the provider returned (e.g. 200, 404,
    412, 429, 503). ``result`` -- the success envelope on a 2xx (with
    ``next_cursor`` for paging and the ``payload`` carrying the items / object /
    bytes the ``decode`` produced), else ``None``. ``preconditions`` -- the
    precondition names that failed on a 412 (``("If-Match",)`` etc.), empty
    otherwise. ``etag`` -- the server's CURRENT ETag on a 412 (what a readback
    would re-derive against), else ``None``. ``retry_after_seconds`` -- the
    server's advisory backoff on a 429/503, else ``None``. ``detail`` -- a short
    provider message; it is redacted by :func:`operation_error` before it ever
    leaves the executor.

    ``write_outcome`` -- what is now KNOWN about whether a non-idempotent
    write's effect landed, as one of L07's :data:`AttemptOutcome` values, or
    ``None`` when the transport makes no claim. It exists because the wire can
    fail in a way that answers NOTHING: a socket timeout or a connection dropped
    mid-flight means the request may have been received and applied, or may never
    have arrived, and the transport is the only layer positioned to tell that
    apart from a status the provider deliberately returned. Recording such a
    failure as ``failed_not_applied`` would hand
    :func:`~kiro_crew.connections.control_plane.writes.replay_decision` a
    determinate "it did not land" -- the one branch L07 ALLOWS replaying -- so a
    timeout would license a second ``sendMail``. A transport that cannot know
    says ``unknown`` here, and L07 then refuses to blind-replay unless the caller
    explicitly asserts the operation is idempotent.

    ``metadata`` -- the ALLOWLISTED response metadata (see
    :data:`ResponseMetadata`), lowercase-keyed and read-only. It is how the
    rate-limit family reaches a caller, and it is an allowlist precisely so that
    ``Set-Cookie`` / ``WWW-Authenticate`` / an echoed ``Authorization`` cannot: a
    response header set is a credential surface, and a transport that handed the
    whole set over would leak session material into whatever a caller does with
    its results. Empty on any path that produced no response.
    """

    http_status: int
    result: Optional[OperationResult] = None
    preconditions: Tuple[str, ...] = ()
    etag: Optional[str] = None
    retry_after_seconds: Optional[float] = None
    detail: str = ""
    write_outcome: Optional[str] = None
    metadata: ResponseMetadata = EMPTY_RESPONSE_METADATA


# --- the structured 412 signal the caller consumes (NOT flattened) ------------
@dataclass(frozen=True)
class PreconditionFailure:
    """A structured HTTP 412 signal -- preserved, never collapsed to ``conflict``.

    A 412 means the precondition the caller ASSERTED (an ``If-Match`` ETag, an
    ``If-Unmodified-Since``) does not hold, so the write did NOT apply. The
    caller must be able to tell this apart from a generic error and get enough
    context to decide "re-read and re-derive the precondition, then retry"
    rather than blind-retry.

    IMPORTANT: a 412 does NOT tell you the server's current state -- only that
    your asserted precondition is false. ``failed_not_applied`` here records that
    the write did not land (the branch L07 *allows* replaying); it is NOT a
    licence to reissue the identical request. Re-read (readback), re-derive the
    precondition against ``server_etag``, and only THEN retry. MS owns its own
    baseline / fresh / locator revalidation on top of this signal.

    ``preconditions`` -- the failed precondition names EXACTLY as the transport
    reported them, which may legitimately be the EMPTY tuple: a provider that
    returns a bare 412 does not say which condition failed, and naming
    ``If-Match`` anyway would invent a readback contract the vendor owner owns.
    ``condition_unknown`` -- True precisely when the transport named no
    precondition AND sent no ETag, i.e. nothing here identifies what failed, so
    the caller must read the resource back to find out rather than re-assert a
    guess. ``server_etag`` -- the provider's current ETag to re-derive against
    (``None`` if the provider sent none). ``recorded_outcome`` -- always
    ``failed_not_applied``. ``error`` -- the typed L01 ``conflict``
    :class:`OperationError` (redacted detail) for a caller that only wants the
    flat class; the structured fields above are what makes the signal
    discriminable.
    """

    preconditions: Tuple[str, ...]
    server_etag: Optional[str]
    error: OperationError
    recorded_outcome: str = ATTEMPT_FAILED_NOT_APPLIED
    condition_unknown: bool = False


# --- the executor's own result envelope ---------------------------------------
@dataclass(frozen=True)
class ExecutionOutcome:
    """The executor's uniform return for one ``execute`` / ``advance_page`` call.

    Exactly one of ``result`` / ``error`` / ``precondition`` is set.

    ``result`` -- the success envelope when the call was authorized, emitted, and
    returned 2xx. It carries the single authoritative pagination ``next_cursor``
    AND the ``payload``: the items / object / bytes the operation returned (see
    :data:`~kiro_crew.connections.control_plane.result.OperationPayload`). Read
    the payload through :attr:`payload` rather than indexing ``result``.
    ``error`` -- a typed
    :class:`OperationError` when a gate denied (transport NOT called) or the
    transport returned a non-precondition failure. ``precondition`` -- the
    structured :class:`PreconditionFailure` on a 412. ``view`` -- the trusted
    :class:`TrustedHandleView` the routing decision used, present whenever the
    gate chain got far enough to resolve it (so a caller/audit can see the
    trusted axes); ``None`` when the handle itself was rejected.

    ``write_outcome`` -- the transport's
    :attr:`TransportResponse.write_outcome` carried through unchanged: the L07
    :data:`~kiro_crew.connections.control_plane.writes.AttemptOutcome` a caller
    must record for THIS attempt, or ``None`` when nothing was claimed (a gate
    denial, a read, a clean 2xx). This is what a caller writes into the
    :class:`~kiro_crew.connections.control_plane.writes.AttemptRecord` it keeps
    for the next attempt. Carrying it is the point: an executor that dropped it
    would leave the caller inferring "not applied" from a 5xx, which is the
    precise inference L07 exists to refuse.

    ``metadata`` -- the transport's :attr:`TransportResponse.metadata` carried
    through unchanged: the ALLOWLISTED response metadata (rate-limit family and a
    few benign headers), lowercase-keyed and read-only. This is the ONE surface
    response headers reach a caller through, and everything not on
    :data:`~kiro_crew.connections.control_plane.production.RESPONSE_METADATA_ALLOWLIST`
    -- every credential and session header included -- is absent by construction.
    Empty on a denied gate, since no response exists to describe.
    """

    result: Optional[OperationResult] = None
    error: Optional[OperationError] = None
    precondition: Optional[PreconditionFailure] = None
    view: Optional[TrustedHandleView] = None
    write_outcome: Optional[str] = None
    metadata: ResponseMetadata = EMPTY_RESPONSE_METADATA

    @property
    def ok(self) -> bool:
        return self.result is not None and self.error is None and self.precondition is None

    @property
    def payload(self) -> Optional[OperationPayload]:
        """The DATA this call returned, or ``None`` when it returned none.

        The consumer-facing read of the neutral data channel: one of
        :class:`~kiro_crew.connections.control_plane.result.CollectionPayload`,
        :class:`~kiro_crew.connections.control_plane.result.ObjectPayload`,
        :class:`~kiro_crew.connections.control_plane.result.BytesPayload`, or
        ``None``. A denied gate, a transport error and a 412 all have no result at
        all, so they read ``None`` here rather than raising -- a caller that
        already branched on :attr:`ok` does not have to branch again.

        ``.get`` rather than ``["payload"]`` on purpose: the key is REQUIRED on a
        v2 envelope, but ``OperationResult`` is a ``TypedDict`` and nothing
        validates a plain dict at runtime, so a producer still pinned to v1 hands
        back a two-key mapping. Reading that as "no payload" is the honest answer;
        raising ``KeyError`` deep in a consumer would not be.
        """

        if self.result is None:
            return None
        return self.result.get("payload")


# --- the injected transport contract ------------------------------------------
#: A transport takes the trusted routing axes plus the request and returns a
#: :class:`TransportResponse`. It is called ONLY after every gate has passed. The
#: executor passes the TRUSTED ``service_id`` / ``credential_mode`` from the
#: handle view -- never the handle's own -- so the thing that reaches the network
#: cannot be pointed elsewhere by a mutated handle.
#:
#: It is also handed ``trusted_view``, the :class:`TrustedHandleView` itself, so
#: a transport whose custody is per-binding can bind the credential it resolves
#: to the identity this call was AUTHORIZED for. Passing only the two routing
#: axes was not enough: two bindings can share a ``service_id`` and a
#: ``credential_mode`` and still be different accounts/tenants, so a transport
#: given only those cannot tell whose secret it should be reaching for and would
#: reuse whichever one it was composed with. The view's ``binding_fingerprint``
#: is the trusted, non-invertible link back to the binding, and it is what
#: :class:`~kiro_crew.connections.control_plane.production.BindingCustodyGate`
#: matches on before any secret is resolved.
Transport = Callable[..., TransportResponse]

#: A SERVER-SIDE clock: called with no arguments, returns POSIX seconds. This is
#: how the executor learns the time -- it is called fresh per :func:`execute` and
#: per :class:`PageWalk` page, so no caller-asserted instant is carried forward
#: across a walk and expiry is re-judged every time. Defaults to
#: :func:`time.time`; a deterministic test injects its own.
Clock = Callable[[], float]


def _finite(now: float) -> bool:
    return isinstance(now, (int, float)) and math.isfinite(now)


def _classify_status(status: int, detail: str) -> Optional[ErrorClass]:
    """Map an HTTP status to an L01 :class:`ErrorClass`, or ``None`` for 2xx.

    Pure, table-driven. 412 is deliberately NOT in this table: a precondition
    failure is handled as a structured :class:`PreconditionFailure`, not a flat
    error class, so it is never collapsed to ``conflict`` here.
    """

    if 200 <= status < 300:
        return None
    # Annotated so the table's values narrow to ErrorClass rather than to str;
    # without it mypy reads the dict as dict[int, str] and rejects the return.
    table: dict[int, ErrorClass] = {
        400: "input",
        401: "auth",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        429: "throttle",
    }
    fallback: ErrorClass = "temporary" if status >= 500 else "input"
    return table.get(status, fallback)


def classify_error(response: TransportResponse) -> Optional[OperationError]:
    """Public error-classification surface for downstreams (GitHub/MS).

    Returns ``None`` for a 2xx, a structured signal is NOT its job (412 is
    surfaced by :func:`execute`); for every other non-2xx it returns a typed
    :class:`OperationError` whose ``detail`` is redacted by
    :func:`operation_error`. Downstream error handling pins
    :data:`EXECUTOR_SCHEMA_VERSION` and switches on the returned ``class_``.
    """

    error_class = _classify_status(response.http_status, response.detail)
    if error_class is None:
        return None
    suffix = ""
    if response.retry_after_seconds is not None:
        suffix = f" (retry_after={response.retry_after_seconds}s)"
    return operation_error(
        error_class,
        f"transport returned HTTP {response.http_status}{suffix}",
    )


def _authorize(
    descriptor: OperationDescriptor,
    handle: DerivedHandle,
    *,
    now: float,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    attempt_record: Optional[AttemptRecord],
    request_args: Mapping[str, Any],
    request_idempotency_key: str,
) -> Tuple[Optional[TrustedHandleView], Optional[OperationError], Optional[ReplayDecision]]:
    """Run the four judgments IN ORDER. Returns as soon as one denies.

    Between step 1 (the trusted view) and step 2 (the permit) sits step 1b: the
    caller's ``offered_mode`` and the descriptor's ``service_id`` are compared to
    the view and a disagreement is refused. That check has to be here, not in
    :func:`permit_operation` -- the permit only sees the descriptor and the
    policy, never the handle -- and it has to be before step 2, so a mismatched
    mode never reaches a decision that would happily allow it.

    On allow: ``(view, None, replay_or_None)``. On deny: ``(view_or_None,
    error, None)`` -- ``view`` is set once step 1 resolved it, so an audit sees
    the trusted axes even on a later deny. The transport is the caller's job and
    is only reached when this returns no error.
    """

    # (0) The clock must be finite before any time-sensitive judgment. L08's
    # ensure_usable re-checks this, but refusing here keeps a bad clock from
    # ever reaching a gate.
    if not _finite(now):
        return (
            None,
            operation_error("input", f"now must be a finite POSIX timestamp, got {now!r}"),
            None,
        )

    # (1) Trusted handle view -- the ONLY source of service_id / credential_mode.
    try:
        view = ensure_usable(handle, now=now)
    except (HandleExpiredError, HandleNotIssuedError, HandleTamperedError, HandleScopeError) as exc:
        return None, exc.error, None

    # (1b) The caller's own claims must AGREE with the trusted view before any
    # permit decision is made. permit_operation judges the mode the CALLER
    # offered against the descriptor and the policy -- it has no idea which mode
    # this handle was actually issued for -- and the descriptor names the service
    # the operation is for. Both are caller-supplied, so validating the handle and
    # then authorizing a different mode / service would be the same "validate,
    # then use the unvalidated value" hole one layer up:
    #
    #   * a handle issued for oauth_user + an offered service_to_service passes
    #     permit_operation whenever the descriptor declares BOTH modes, and would
    #     then be emitted under the handle's oauth_user credential;
    #   * a descriptor naming github against an outlook handle would be
    #     authorized as a github operation and routed at outlook.
    #
    # Refuse both, before the transport exists as a possibility.
    if offered_mode != view.credential_mode:
        return (
            view,
            operation_error(
                "auth",
                f"offered credential mode '{offered_mode}' does not match the mode "
                f"this handle was issued for ('{view.credential_mode}'); the "
                "trusted view decides which credential authenticates the call",
            ),
            None,
        )
    if descriptor["service_id"] != view.service_id:
        return (
            view,
            operation_error(
                "auth",
                f"operation '{descriptor['operation_id']}' names service "
                f"'{descriptor['service_id']}' but this handle was issued for "
                f"'{view.service_id}'; a handle is never a basis to act on "
                "another service",
            ),
            None,
        )

    # (2) Credential-mode permit (deny-by-default, unstated == denied).
    auth_error = permit_operation(descriptor, offered_mode, permitted)
    if auth_error is not None:
        return view, auth_error, None

    # (3) Five-layer governance intersection (unknown scope == deny).
    permitted_by_policy, policy_error = decide(layers, governance_scope, governance_item)
    if not permitted_by_policy:
        return view, policy_error, None

    # (4) Write-replay gate -- only for a non-idempotent write that carries a
    # prior attempt record. A first attempt (no record) is not a replay.
    replay: Optional[ReplayDecision] = None
    if is_non_idempotent_effect(descriptor["effect"]) and attempt_record is not None:
        replay = replay_decision(
            descriptor,
            attempt_record,
            request_args=dict(request_args),
            request_idempotency_key=request_idempotency_key,
        )
        if replay["verdict"] == REPLAY_REFUSE:
            return view, replay["error"], None
        if replay["verdict"] == REPLAY_REUSE:
            # A prior success recorded: hand back the recorded result, do NOT
            # reissue (that would duplicate the effect). Signalled to execute()
            # via the replay decision.
            return view, None, replay

    return view, None, replay


def _audit_authorization(
    *,
    descriptor: OperationDescriptor,
    view: Optional[TrustedHandleView],
    outcome: str,
    error: Optional[OperationError],
) -> None:
    """Emit ONE SEL security event for a final authorization decision.

    Anchor: ``backend-security-controls``. Every ``execute`` call reaches exactly
    one of ``allow`` (about to dispatch) or ``deny`` (refused before any emit),
    and each records a SEL event so the authorization decision leaves an audit
    trail. The identity is the view's ``binding_fingerprint`` -- a one-way keyed
    digest, never a secret and never invertible into a binding -- plus the trusted
    routing axes; a denied call whose handle never resolved has no view, and the
    event says so rather than inventing one. The redacted ``error_class`` (never
    the detail) rides a deny.

    Soft-fail like the other SEL emitters in the tree: the security log being down
    must not turn a decided call into an exception. ``kiro_crew.sel`` is
    stdlib-only and in the same package, so it is imported at module scope (no
    import-time dependency risk); only its USE is guarded, so a runtime SEL fault
    still soft-fails rather than failing a decided call.
    """

    try:
        resources = f"operation={descriptor.get('operation_id', '?')}"
        if view is not None:
            resources += (
                f" service={view.service_id} mode={view.credential_mode}"
                f" binding_fp={view.binding_fingerprint}"
            )
        else:
            resources += " binding=unresolved"
        if outcome == "deny" and error is not None:
            resources += f" error_class={error.get('error_class', '?')}"
        sel().log(
            SecurityEvent(
                event_id=os.urandom(8).hex(),
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="connections.operation.authorization",
                caller_identity=(view.binding_fingerprint if view is not None else "unresolved"),
                agent="kirocrew",
                source="connections.control_plane",
                operation=str(descriptor.get("operation_id", "?")),
                outcome=outcome,
                resources=resources,
            )
        )
    except Exception as exc:  # SEL down must not fail a decided call.
        logging.getLogger(__name__).warning(
            "SEL audit for connections authorization failed: %s", exc
        )


def execute(
    descriptor: OperationDescriptor,
    handle: DerivedHandle,
    transport: Transport,
    *,
    now: Optional[float] = None,
    clock: Clock = time.time,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
    layers: LayerCeilings,
    governance_scope: str,
    governance_item: str,
    request_args: Optional[Mapping[str, Any]] = None,
    request_idempotency_key: str = "",
    attempt_record: Optional[AttemptRecord] = None,
) -> ExecutionOutcome:
    """Authorize, then (only if authorized) emit ONE call through ``transport``.

    The gate chain (handle view -> caller/view agreement -> credential mode ->
    governance -> write replay) runs FIRST. If any gate denies, this returns an
    :class:`ExecutionOutcome` carrying the typed error and the transport is NEVER
    called. Routing uses the TRUSTED view's ``service_id`` / ``credential_mode``,
    not the handle's, and the caller's own claims about those two axes must agree
    with the view (see :func:`_authorize` step 1b).

    **Time comes from ``clock``, called fresh on every invocation** -- a
    server-side reading, not an instant the caller asserted and can hold still.
    ``now`` overrides it and exists for deterministic tests ONLY; unset (the
    default) is the production shape, and it is what makes a long paging walk
    re-judge expiry per page instead of once.

    A ``reuse`` replay verdict hands back the recorded result without calling the
    transport. On a real emit, an HTTP 412 is returned as a structured
    :class:`PreconditionFailure` (never flattened); every other non-2xx becomes a
    typed error; a 2xx returns the success envelope, carrying the transport's
    ``payload`` (items / object / bytes) through to
    :attr:`ExecutionOutcome.payload` unchanged.
    """

    args: Mapping[str, Any] = request_args or {}
    # Derive the instant HERE, per call, from the clock -- unless a test pinned
    # one. A non-finite reading (from either source) is refused by _authorize.
    resolved_now = clock() if now is None else now
    view, error, replay = _authorize(
        descriptor,
        handle,
        now=resolved_now,
        offered_mode=offered_mode,
        permitted=permitted,
        layers=layers,
        governance_scope=governance_scope,
        governance_item=governance_item,
        attempt_record=attempt_record,
        request_args=args,
        request_idempotency_key=request_idempotency_key,
    )
    if error is not None:
        _audit_authorization(descriptor=descriptor, view=view, outcome="deny", error=error)
        return ExecutionOutcome(error=error, view=view)

    # Authorized. A recorded success is reused WITHOUT emitting (no duplicate).
    if replay is not None and replay["verdict"] == REPLAY_REUSE:
        _audit_authorization(descriptor=descriptor, view=view, outcome="allow", error=None)
        return ExecutionOutcome(result=replay["reuse_result"], view=view)

    # Emit exactly one call, routed on the TRUSTED axes. The view goes along too:
    # a per-binding transport must be able to bind its credential custody to the
    # identity this call was authorized for, and the two routing axes alone do
    # not identify a binding (two bindings can share both).
    assert view is not None  # invariant: no error means the view resolved
    # No SEL event yet: the gate authorized, but the transport still runs its own
    # custody / live-store fence (a revoked binding, a stale generation) and can
    # REFUSE. Auditing "allow" here would record a call the fence then denied. The
    # decision is emitted AFTER the transport returns, from its actual outcome, so
    # the audit never contradicts what happened.
    response = transport(
        service_id=view.service_id,
        credential_mode=view.credential_mode,
        trusted_view=view,
        descriptor=descriptor,
        request_args=dict(args),
        request_idempotency_key=request_idempotency_key,
    )

    # HTTP 412: preserve the structured precondition signal, do not flatten. The
    # provider answered (the call reached the wire), so it is an allow.
    if response.http_status == 412:
        _audit_authorization(descriptor=descriptor, view=view, outcome="allow", error=None)
        return ExecutionOutcome(
            precondition=_precondition_failure(response),
            view=view,
            write_outcome=response.write_outcome,
            metadata=response.metadata,
        )

    error = classify_error(response)
    if error is not None:
        # A transport `auth` error is the custody / live-store fence REFUSING (the
        # binding was revoked, stale, or is not the one this transport holds), so
        # it is a DENY -- the case F3 was about. Any other error (temporary /
        # throttle / ...) means the call WAS authorized and reached the wire, so
        # it is an allow that then failed downstream.
        _audit_authorization(
            descriptor=descriptor,
            view=view,
            outcome=("deny" if error["error_class"] == "auth" else "allow"),
            error=error,
        )
        # The transport's outcome claim rides along with the failure -- this is
        # the case that matters: a timeout on a non-idempotent write is a typed
        # `temporary` error AND an `unknown` outcome, and dropping the second
        # would let the caller record the first as "not applied".
        #
        # The metadata rides along too, and the FAILURE path is where it earns
        # its keep: a 429's `retry-after` and `x-ratelimit-reset` are only ever
        # on a response the caller is about to back off from.
        return ExecutionOutcome(
            error=error,
            view=view,
            write_outcome=response.write_outcome,
            metadata=response.metadata,
        )

    _audit_authorization(descriptor=descriptor, view=view, outcome="allow", error=None)
    return ExecutionOutcome(
        result=response.result,
        view=view,
        write_outcome=response.write_outcome,
        metadata=response.metadata,
    )


def _precondition_failure(response: TransportResponse) -> PreconditionFailure:
    """Build the structured 412 signal from a transport response.

    Reports what the transport ACTUALLY said. When it named no failed
    precondition, ``preconditions`` stays empty rather than defaulting to
    ``("If-Match",)``: fabricating a name asserts a readback contract this module
    does not own (the vendor owner does) and would point the caller at a
    condition it may never have sent. With no name AND no ETag there is nothing
    to re-derive against at all, so ``condition_unknown`` is set and the detail
    says the caller must read the resource back.
    """

    preconditions = tuple(response.preconditions)
    condition_unknown = not preconditions and response.etag is None
    if preconditions:
        detail = (
            "precondition failed: the asserted precondition(s) "
            + ", ".join(preconditions)
            + " do not hold; the write did not apply -- re-read and re-derive the "
            "precondition before any retry"
        )
    elif condition_unknown:
        detail = (
            "precondition failed: the provider named no failed precondition and "
            "sent no ETag, so WHICH condition failed is not known here; the write "
            "did not apply -- read the resource back to establish the current "
            "state before any retry"
        )
    else:
        detail = (
            "precondition failed: the provider named no failed precondition; the "
            "write did not apply -- re-derive against the returned ETag and "
            "re-read before any retry"
        )
    error = operation_error("conflict", detail)
    return PreconditionFailure(
        preconditions=preconditions,
        server_etag=response.etag,
        error=error,
        condition_unknown=condition_unknown,
    )


# --- pagination: real cursor advance, never a placeholder ---------------------
@dataclass
class PageWalk:
    """A real, terminating pagination walk over ``execute``.

    Each :meth:`next` call runs the FULL gate chain again (a cursor does not
    exempt a page from authorization) and emits one call carrying the current
    cursor. It advances on the response's ``next_cursor``, stops when that is
    ``None``, and refuses to loop forever on a repeated cursor -- so it neither
    drops nor duplicates a page and always terminates.

    **ONE cursor source.** That ``next_cursor`` is read from
    :attr:`~kiro_crew.connections.control_plane.result.OperationResult.next_cursor`
    -- the envelope -- and from nowhere else. The collection payload does not
    carry a second copy to read instead (see
    :class:`~kiro_crew.connections.control_plane.result.CollectionPayload`),
    because a walk with two candidate cursors has a losing branch that is
    invisible from here: reading the one that a middle layer did not update stops
    the walk early (records silently dropped) or repeats a page (records silently
    duplicated), and both look exactly like a correct walk to this class.

    **The walk holds a ``clock``, not an instant.** It stores no frozen ``now``:
    every page re-reads ``clock()`` and re-runs the gate chain against that fresh
    reading, so a handle that expires between page 2 and page 3 stops the walk at
    page 3. A cached instant would have made the whole walk -- however long -- run
    on the expiry judgment made at page 1, and finiteness checks cannot notice
    that, because a stale instant is a perfectly finite number. A deterministic
    test injects its own callable (e.g. one that advances on each call).

    **Every page carries the ORIGINAL request args.** ``base_args`` holds the
    filters/selectors the walk was opened with, and each page sends
    ``{**base_args, "cursor": cursor}``. Sending the cursor alone would silently
    drop the filter set from page 2 onward -- a different query than page 1 --
    and would change the request's ``args_fingerprint``, so L07's attribution
    would fail to recognize a retry of a page as the same logical request.

    ``done`` is True once the last page returned no ``next_cursor`` (or a gate
    denied / a transport error stopped the walk). ``pages`` counts the pages
    successfully fetched.
    """

    descriptor: OperationDescriptor
    handle: DerivedHandle
    transport: Transport
    offered_mode: CredentialMode
    permitted: PermittedModes
    layers: LayerCeilings
    governance_scope: str
    governance_item: str
    clock: Clock = time.time
    base_args: Mapping[str, Any] = field(default_factory=dict)
    _cursor: Optional[str] = None
    done: bool = False
    pages: int = 0
    _seen_cursors: set = field(default_factory=set)

    def next(self) -> ExecutionOutcome:
        """Fetch the next page. Returns the outcome; sets ``done`` at the end."""

        if self.done:
            raise StopIteration("pagination walk is already complete")

        # A repeated cursor would loop forever: terminate defensively.
        if self._cursor is not None and self._cursor in self._seen_cursors:
            self.done = True
            return ExecutionOutcome(
                error=operation_error(
                    "temporary",
                    "pagination refused to advance: the provider returned a "
                    "cursor already seen this walk (would loop)",
                )
            )
        if self._cursor is not None:
            self._seen_cursors.add(self._cursor)

        outcome = execute(
            self.descriptor,
            self.handle,
            self.transport,
            # No `now`: execute() reads self.clock FRESH for this page, so expiry
            # is re-judged here rather than inherited from page 1.
            clock=self.clock,
            offered_mode=self.offered_mode,
            permitted=self.permitted,
            layers=self.layers,
            governance_scope=self.governance_scope,
            governance_item=self.governance_item,
            # The cursor is appended to the ORIGINAL args, never sent instead of
            # them; it wins on a key collision because it is this page's cursor.
            request_args={**dict(self.base_args), "cursor": self._cursor},
            request_idempotency_key="",
        )

        if not outcome.ok:
            # A denied gate, a transport error, or a 412 stops the walk.
            self.done = True
            return outcome

        self.pages += 1
        # The SINGLE authoritative cursor: the envelope's. Never a payload's -- a
        # CollectionPayload has none, and that absence is what makes this line the
        # only place a walk can learn where to resume.
        next_cursor = outcome.result["next_cursor"] if outcome.result else None
        if next_cursor is None:
            self.done = True
        else:
            self._cursor = next_cursor
        return outcome


def advance_page(walk: PageWalk) -> ExecutionOutcome:
    """Advance one page of a :class:`PageWalk` (the public paging surface)."""

    return walk.next()


__all__ = [
    "EMPTY_RESPONSE_METADATA",
    "EXECUTOR_SCHEMA_VERSION",
    "Clock",
    "ExecutionOutcome",
    "PageWalk",
    "PreconditionFailure",
    "ResponseMetadata",
    "Transport",
    "TransportResponse",
    "advance_page",
    "classify_error",
    "execute",
    "is_non_idempotent_effect",
]
