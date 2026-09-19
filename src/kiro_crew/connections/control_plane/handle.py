"""W01 · L08: a restricted, short-lived handle DERIVED from a trusted binding.

L02 (:mod:`kiro_crew.connections.control_plane.binding`) fixed the record a
``binding_ref`` points at: a :class:`~kiro_crew.connections.control_plane.binding.Binding`
is a LONG-LIVED, FULL-AUTHORITY thing -- it names a verified subject/tenant and
references the connection's secret, and it never expires. Handing a call site
the whole binding hands it the connection's entire authority, forever. This
module derives a NARROWER, EXPIRING, NON-REVERSIBLE handle from a binding, so a
downstream consumer (W05's Microsoft Graph stream is the first) works under a
capability that is strictly less than the binding and cannot be turned back into
it. It MINTS a handle from a binding and it DECIDES whether a handle may be used;
it does not persist across processes, resolve one to a credential, revoke one,
or reach kiro-cli.

The trust model: a handle is a claim, the issuance record is the authority
====================================================================
A :class:`DerivedHandle` is an ordinary (mutable) ``TypedDict`` a caller holds
and passes around. It is therefore **not trusted**: a caller can edit its
``scopes``, push its ``not_after``, or change its ``generation`` in the dict. So
NO enforcement decision is ever read off the handle's own fields. Every mint
records the authoritative facts -- scope set, expiry, generation, binding
fingerprint -- in a module-private ISSUANCE REGISTRY keyed by the random
``handle_id`` (:data:`_ISSUED`), and :func:`ensure_usable` judges ONLY against
that record:

- it looks the handle up by ``handle_id``; a handle whose id is not in the
  registry is REFUSED (fail closed) -- this is exactly what makes an unknown,
  forged, cross-process, or post-restart handle unusable (see below);
- it decides expiry from the RECORD's ``not_after``, scope from the RECORD's
  scope set, generation from the RECORD's generation -- never the handle's
  self-reported copies;
- if the presented handle DISAGREES with the record on ANY axis a caller could
  abuse (a wider scope, a later expiry, a different generation, fingerprint,
  ``service_id`` or ``credential_mode``) it is REFUSED as tampered, rather than
  silently judged on the record and let through -- the mismatch is surfaced, not
  swallowed;
- it also REFUSES a non-finite ``now``: a ``NaN`` clock would make the expiry
  comparison silently False and let an expired handle through, so the enforcement
  never trusts the caller's clock without checking it is finite first;
- on success it RETURNS a :class:`TrustedHandleView` built from the RECORD (not
  the handle). **A consumer routes on this return value -- its ``service_id`` /
  ``credential_mode`` / ``scopes`` -- and MUST NOT read those off the mutable
  handle.** Returning ``None`` and leaving the caller to read the dict for
  routing would reopen the "validate, then use the unvalidated value" hole: a
  caller who flipped ``service_id`` to another provider would send the call
  there. The trusted view is the answer; the handle is only a claim.

Cross-process and restart semantics (a contract, fail-closed)
-------------------------------------------------------------
The issuance registry is PROCESS-LOCAL and IN-MEMORY: it starts empty at import
and is never persisted. Two consequences are guaranteed, not undefined:

- **After a restart, every handle minted before the restart is refused.** The
  new process starts with an empty registry, so no prior ``handle_id`` resolves,
  and :func:`ensure_usable` fails closed. A pre-restart handle is never silently
  accepted and never treated as valid.
- **A handle minted in another process is refused here.** Each process has its
  own registry, so a ``handle_id`` issued elsewhere is absent from this one and
  is refused identically.

This is the capability boundary: a derived handle is usable ONLY within the
process that issued it, for as long as that process lives and the record has not
expired. A handle is not a bearer token that survives serialization to another
process; that is deliberate, and it is enforced by the lookup, not documented as
a hope. (Cross-process handoff, if a later slice needs it, is a signed-record or
shared-store design that is explicitly out of this slice.)

The three invariants, each enforced by real code with a real refusal path
--------------------------------------------------------------------------
1. **Scope only narrows, never widens.** At mint, :func:`derive_handle` refuses
   a requested scope the binding does not grant (typed ``scope`` error, no
   handle minted). At use, :func:`ensure_usable` refuses a handle whose
   presented ``scopes`` are not a subset of the RECORD's scopes -- so a caller
   who widens the dict after minting is refused, not obeyed.
2. **An expired handle is refused.** :func:`ensure_usable` compares the presented
   ``now`` against the RECORD's ``not_after`` (inclusive) and refuses with a
   typed ``input`` error -- pushing the handle's own ``not_after`` changes
   nothing, because the decision reads the record. A non-finite or overflowing
   TTL is refused at mint (see :func:`derive_handle`), so ``not_after`` is always
   a finite, comparable instant.
3. **The handle cannot reconstruct the binding.** A handle carries NOTHING that
   rebuilds its binding: not ``binding_id``, not ``subject_ref`` / ``tenant_ref``,
   not the ``secret_ref``. Its ``handle_id`` is random. The one link back is
   ``binding_fingerprint`` -- a ONE-WAY keyed digest (HMAC-SHA256 under a
   per-process random key) of ``binding_id``, matchable by L04 revoke fencing but
   not invertible.

Why ``generation`` is carried but ``binding_id`` is not
-------------------------------------------------------
L04's revoke fencing (NOT implemented here) needs the handle's ``generation``
(to compare against the binding's current one) and a way to tell WHICH binding
the handle came from. Carrying the raw ``binding_id`` would answer the second
but break invariant 3, so the record and handle carry the non-invertible
``binding_fingerprint`` instead. This slice lands ``generation`` + the
fingerprint and proves they are preserved; it makes no revoke decision.

Secret custody is NOT this slice's
----------------------------------
This module handles REFERENCES and DERIVATION only. It never reads, copies, or
holds a secret value, and it does not touch the vault: the ``secrets/vault.py``
``SecretVault`` / ``SecretValue`` machinery and the
``oauth_clients.client_secret_name(slug)`` naming convention are the existing
custody mechanism, and a handle deliberately carries no ``secret_ref`` at all --
resolving a binding's secret stays a later leaf's job, gated by the handle's
scope and expiry rather than reachable from the handle itself.

Typed refusals reuse L01's :func:`~kiro_crew.connections.control_plane.errors.operation_error`
(which redacts its detail unconditionally); this module opens no error channel
of its own, exactly as ``policy.py`` / ``auth_modes.py`` / ``writes.py`` do.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Tuple, TypedDict

from kiro_crew.connections.control_plane.binding import Binding
from kiro_crew.connections.control_plane.errors import (
    OperationError,
    operation_error,
)
from kiro_crew.connections.control_plane.operation import CredentialMode, ServiceId

#: Bumped when this module's shape changes, mirroring the L01/L02 sibling
#: control-plane modules (``operation`` / ``context`` / ``result`` / ``errors``
#: / ``binding`` each pin one).
HANDLE_SCHEMA_VERSION = 2

#: Bytes of randomness behind ``handle_id``. 16 bytes = 128 bits = a 32-char hex
#: string -- the same unguessable-handle strength ``binding._BINDING_ID_BYTES``
#: uses, and far past any enumeration budget.
_HANDLE_ID_BYTES = 16

#: Bytes of the per-process key that keys the ``binding_fingerprint`` HMAC. The
#: key is random per process and never leaves it, so the fingerprint is a
#: one-way digest no handle holder can invert or recompute from a guessed
#: ``binding_id``. It is regenerated each process start on purpose: fingerprints
#: only ever need to be comparable to OTHER fingerprints minted in the same
#: process (L04's fencing recomputes under the live key), never persisted or
#: matched across restarts.
_FINGERPRINT_KEY_BYTES = 32

#: The per-process HMAC key. Minted once at import from a CSPRNG; module-private
#: so nothing outside this module can read it and thereby forge or invert a
#: fingerprint.
_FINGERPRINT_KEY = secrets.token_bytes(_FINGERPRINT_KEY_BYTES)


def _binding_fingerprint(binding_id: str) -> str:
    """Return the one-way keyed digest of ``binding_id`` for L04 fencing.

    HMAC-SHA256 under the module's per-process key. The result is deterministic
    within a process (so two handles derived from the SAME binding share a
    fingerprint, which is what lets L04 match them) but is NOT invertible to the
    ``binding_id`` and cannot be recomputed by anyone who does not hold the
    process key. This is the ONLY link a handle keeps back to its binding, and
    it deliberately reveals nothing about the id it digests.
    """

    return hmac.new(_FINGERPRINT_KEY, binding_id.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _IssuanceRecord:
    """The TRUSTED authority for one issued handle, held server-side.

    This is what :func:`ensure_usable` judges against -- never the handle's own
    fields. It is frozen so nothing can mutate an issued record in place, and it
    lives only in the module-private :data:`_ISSUED` registry keyed by
    ``handle_id``. It records the authoritative service range, credential mode,
    scope set, expiry, generation and binding fingerprint decided at mint; a
    caller holds a copy of these on the handle but the copy has no authority.
    """

    handle_id: str
    service_id: ServiceId
    credential_mode: CredentialMode
    scopes: FrozenSet[str]
    generation: int
    binding_fingerprint: str
    not_after: float


@dataclass(frozen=True)
class TrustedHandleView:
    """The TRUSTED, record-sourced view :func:`ensure_usable` returns.

    Every field is copied from the issuance :class:`_IssuanceRecord`, NOT from
    the caller-held handle, and the view is frozen so it cannot be edited after
    it is handed back. This is the object a consumer (W05's Graph stream, L09's
    router) MUST route on: it decides which service to reach (``service_id``) and
    which credential to authenticate with (``credential_mode``) from values the
    caller could not have altered. Reading those off the mutable handle instead
    is exactly the "validate then use the unvalidated value" hole this return
    value closes.

    ``handle_id`` -- the id the view was resolved for. ``service_id`` /
    ``credential_mode`` -- the trusted routing/auth axes (L01 closed sets).
    ``scopes`` -- the trusted narrowed scope set. ``generation`` /
    ``binding_fingerprint`` -- for L04 fencing. ``not_after`` -- the trusted
    expiry that was enforced.
    """

    handle_id: str
    service_id: ServiceId
    credential_mode: CredentialMode
    scopes: Tuple[str, ...]
    generation: int
    binding_fingerprint: str
    not_after: float


#: The PROCESS-LOCAL issuance registry: ``handle_id`` -> trusted
#: :class:`_IssuanceRecord`. Module-private and in-memory; it starts empty at
#: import and is never persisted, which is exactly what makes a pre-restart or
#: cross-process handle fail closed (its id is absent here). See the module
#: docstring's cross-process/restart contract.
_ISSUED: Dict[str, _IssuanceRecord] = {}


class DerivedHandle(TypedDict):
    """A restricted, short-lived capability derived from a :class:`Binding`.

    Every field present, matching the sibling control-plane descriptors' shape.
    A handle is strictly narrower than the binding it came from and it expires;
    it carries nothing that reconstructs that binding.

    **The handle is a CLAIM, not the authority.** Its fields are a caller-held
    copy of what the issuance record says; a caller may edit them, so no
    enforcement decision reads them -- :func:`ensure_usable` judges against the
    trusted :class:`_IssuanceRecord` keyed by ``handle_id``. A consumer MUST NOT
    read ``scopes`` / ``not_after`` / ``generation`` off the handle to make its
    own allow/deny decision; it MUST call :func:`ensure_usable`.

    ``handle_id`` -- an unguessable random id (see :func:`derive_handle`); NOT
    derived from the binding, and the key into the issuance registry.
    ``service_id`` -- the neutral service range (L01 closed set), for routing; a
    public label, not binding-identifying. ``credential_mode`` -- Axis B: which
    credential the eventual call authenticates with (L01 closed set); a mode
    TYPE, never a value. ``scopes`` -- the narrowed scope set (sorted,
    deduplicated); a display copy of the record's set. ``generation`` -- the
    binding generation this handle was cut from (for L04 fencing).
    ``binding_fingerprint`` -- a one-way keyed digest of the binding's id (see
    :func:`_binding_fingerprint`): the sole link back to the binding, matchable
    by L04 but not invertible. ``issued_at`` / ``not_after`` -- absolute
    POSIX-seconds UTC for when the handle was minted and when it expires (display
    copies; the record's ``not_after`` is the one that is enforced).

    It is a hard invariant of the type that NO binding-reconstructing material
    lives on it: no ``binding_id``, no ``subject_ref`` / ``tenant_ref``, and no
    ``secret_ref`` (or its name). A handle references its binding only through
    the non-invertible ``binding_fingerprint``.
    """

    handle_id: str
    service_id: ServiceId
    credential_mode: CredentialMode
    scopes: Tuple[str, ...]
    generation: int
    binding_fingerprint: str
    issued_at: float
    not_after: float


class HandleScopeError(Exception):
    """Raised when a requested scope is not one the binding grants.

    The one way :func:`derive_handle` refuses to widen: a requested scope
    outside the binding's granted set raises this and no handle is minted. It
    carries the typed L01 :class:`OperationError` (RUN-01 ``scope``) as
    :attr:`error`, so a caller gets the same redacted, typed refusal the rest of
    the control plane speaks, and the message never carries a credential value.
    """

    def __init__(self, error: OperationError) -> None:
        self.error = error
        super().__init__(error["detail"])


class HandleExpiredError(Exception):
    """Raised by :func:`ensure_usable` when a handle is used past its expiry.

    Carries the typed L01 :class:`OperationError` (RUN-01 ``input``) as
    :attr:`error`, so an expired-handle refusal reads as the same redacted,
    typed failure the rest of the control plane speaks. The detail names the
    expiry as the reason (decided from the trusted record) and carries no
    credential value.
    """

    def __init__(self, error: OperationError) -> None:
        self.error = error
        super().__init__(error["detail"])


class HandleNotIssuedError(Exception):
    """Raised by :func:`ensure_usable` when no issuance record backs the handle.

    This is the fail-closed refusal for an unknown, forged, cross-process, or
    post-restart handle: its ``handle_id`` is not in this process's issuance
    registry, so there is no authority to judge it and it is refused. Carries a
    typed L01 :class:`OperationError` (RUN-01 ``auth`` -- the grant behind the
    handle is not recognized here).
    """

    def __init__(self, error: OperationError) -> None:
        self.error = error
        super().__init__(error["detail"])


class HandleTamperedError(Exception):
    """Raised by :func:`ensure_usable` when the handle disagrees with its record.

    The handle presented a scope wider than the record grants, an expiry later
    than the record's, or a different generation / binding fingerprint. Rather
    than silently judging on the trusted record and letting the altered handle
    through, the mismatch is surfaced as a refusal. Carries a typed L01
    :class:`OperationError` (RUN-01 ``auth``).
    """

    def __init__(self, error: OperationError) -> None:
        self.error = error
        super().__init__(error["detail"])


def derive_handle(
    binding: Binding,
    *,
    granted_scopes: Tuple[str, ...],
    requested_scopes: Tuple[str, ...],
    now: float,
    ttl_seconds: float,
) -> DerivedHandle:
    """Derive a narrowed, expiring, non-reversible handle from ``binding``.

    The single sanctioned constructor. In order:

    1. **Finite, positive TTL.** ``ttl_seconds`` and ``now`` MUST be finite (not
       ``NaN`` / ``inf``) and ``ttl_seconds`` MUST be > 0; ``now + ttl_seconds``
       MUST itself be finite (no overflow to ``inf``). Any of these fails with
       :class:`ValueError`. This is what stops a ``NaN`` expiry (whose every
       comparison is False, so it would never look expired) or an ``inf`` expiry
       (a never-expiring handle) from being minted at all.
    2. **Narrow, never widen.** Every scope in ``requested_scopes`` MUST be a
       member of ``granted_scopes``. A requested scope the binding does not grant
       raises :class:`HandleScopeError` (typed ``scope``) and NO handle is
       minted -- never silently dropped. The handle's ``scopes`` are the
       requested set (sorted, deduplicated), a proven subset of ``granted_scopes``.
    3. **Random id + issuance record.** ``handle_id`` is minted from
       :func:`secrets.token_hex`, independent of the binding, and the
       authoritative facts (scope set, expiry, generation, fingerprint) are
       recorded in the process-local :data:`_ISSUED` registry under that id. That
       record -- not the returned handle -- is what :func:`ensure_usable` later
       trusts.
    4. **One-way binding link.** The handle and record carry
       ``binding_fingerprint`` (a keyed, non-invertible digest of
       ``binding['binding_id']``) and ``generation``, never the ``binding_id``
       itself nor the subject/tenant/secret.

    ``granted_scopes`` is supplied by the caller (the binding record itself
    carries no scope set; the authorized-scope set is decided by the layers that
    own it, e.g. L06 governance, and passed in here). This module treats scopes
    as opaque strings and only checks membership; it does not consult, mutate,
    or register anything in ``platform/governance.SCOPE_CATALOG``.
    """

    # (1) TTL must be a finite, positive number and must not overflow to inf.
    # NaN and inf both slip past a bare `<= 0` check (NaN comparisons are always
    # False; inf is > 0), so guard them explicitly BEFORE computing not_after.
    if not math.isfinite(now):
        raise ValueError("now must be a finite POSIX timestamp")
    if not math.isfinite(ttl_seconds):
        raise ValueError("ttl_seconds must be finite (not NaN or inf)")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive; a handle cannot be born expired")
    not_after = now + ttl_seconds
    if not math.isfinite(not_after):
        raise ValueError("now + ttl_seconds overflowed to a non-finite expiry")

    # (2) Narrow, never widen.
    granted = set(granted_scopes)
    over_ask = [scope for scope in requested_scopes if scope not in granted]
    if over_ask:
        error = operation_error(
            "scope",
            "requested scope(s) not granted by the binding: " + ", ".join(sorted(over_ask)),
        )
        raise HandleScopeError(error)

    scopes = tuple(sorted(set(requested_scopes)))
    handle_id = secrets.token_hex(_HANDLE_ID_BYTES)
    fingerprint = _binding_fingerprint(binding["binding_id"])
    generation = binding["generation"]

    # (3) Record the authoritative facts server-side, keyed by handle_id. This
    # record -- not the returned dict -- is the authority ensure_usable trusts.
    _ISSUED[handle_id] = _IssuanceRecord(
        handle_id=handle_id,
        service_id=binding["service_id"],
        credential_mode=binding["credential_mode"],
        scopes=frozenset(scopes),
        generation=generation,
        binding_fingerprint=fingerprint,
        not_after=not_after,
    )

    return {
        "handle_id": handle_id,
        "service_id": binding["service_id"],
        "credential_mode": binding["credential_mode"],
        "scopes": scopes,
        "generation": generation,
        "binding_fingerprint": fingerprint,
        "issued_at": now,
        "not_after": not_after,
    }


def _record_for(handle: DerivedHandle) -> _IssuanceRecord | None:
    """The trusted issuance record for ``handle``, or ``None`` if not issued here.

    A ``None`` return is the fail-closed signal: the handle's ``handle_id`` is
    not in this process's registry (unknown / forged / cross-process /
    post-restart). Callers refuse on ``None``; they never fall back to trusting
    the handle's own fields.
    """

    return _ISSUED.get(handle["handle_id"])


#: The complete set of keys a well-formed handle carries, with the concrete
#: check each must pass to be USABLE by :func:`ensure_usable` -- not merely
#: present, but of a type the enforcement can operate on (a ``handle_id`` that
#: can key the registry, a ``scopes`` that ``set()`` accepts, a ``not_after``
#: that ``>=`` compares). Kept in lock-step with :class:`DerivedHandle`'s fields.
def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _is_int(value: Any) -> bool:
    # bool is an int subclass; a boolean generation/int is malformed, so exclude it.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    # A usable timestamp: a real number (not bool) that ``>``/``>=`` can order
    # against a float. NaN/inf are "numbers" but would break the comparison
    # semantics the expiry check relies on, so they are malformed here too.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _is_scope_sequence(value: Any) -> bool:
    # ``set(value)`` must succeed and yield a scope set: an iterable (but not a
    # bare str, which would iterate into characters) of hashable strings. A
    # missing/non-iterable value or non-string element is what crashes the bare
    # ``set(handle["scopes"])`` today.
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return False
    return all(isinstance(scope, str) for scope in value)


#: Each required key mapped to its usability predicate, in the field order of
#: :class:`DerivedHandle`. A single source of truth for the shape gate.
_HANDLE_FIELD_CHECKS: Tuple[Tuple[str, Any], ...] = (
    ("handle_id", _is_str),
    ("service_id", _is_str),
    ("credential_mode", _is_str),
    ("scopes", _is_scope_sequence),
    ("generation", _is_int),
    ("binding_fingerprint", _is_str),
    ("issued_at", _is_finite_number),
    ("not_after", _is_finite_number),
)


def _validate_handle_shape(handle: Any) -> None:
    """Refuse a malformed handle with a typed :class:`HandleTamperedError`.

    The premise of this module is that a handle's fields are UNTRUSTED. A
    missing or wrong-typed field is the same class of untrusted input as a
    widened scope: a caller who deleted ``scopes`` or set ``not_after`` to a
    string has handed :func:`ensure_usable` something it cannot judge. Left
    unchecked, the first read of such a field raises a bare ``KeyError`` /
    ``TypeError`` that carries no ``error_class``, cannot be routed by an
    upstream on ``error_class``, is not audited, and jumps the whole
    decision chain to land on the caller.

    So the FIRST thing :func:`ensure_usable` does is validate the COMPLETE
    shape: that ``handle`` is a mapping, that every required key is present, and
    that each value is of a type the enforcement can actually operate on (a
    ``handle_id`` that can key the registry, a ``scopes`` that ``set()``
    accepts, a ``not_after`` that ``>=`` can order). Any failure raises
    :class:`HandleTamperedError` (typed ``auth``) instead of crashing. The
    ``detail`` names only the offending FIELD, never its value -- and it goes
    through L01's unconditional redaction regardless.
    """

    if not isinstance(handle, dict):
        error = operation_error(
            "auth",
            "handle is malformed: expected a mapping of handle fields",
        )
        raise HandleTamperedError(error)

    for field, check in _HANDLE_FIELD_CHECKS:
        if field not in handle:
            error = operation_error(
                "auth",
                f"handle is malformed: missing required field {field!r}",
            )
            raise HandleTamperedError(error)
        if not check(handle[field]):
            error = operation_error(
                "auth",
                f"handle is malformed: field {field!r} has an unusable type",
            )
            raise HandleTamperedError(error)


def _handle_id_is_lookupable(handle: Any) -> bool:
    """True iff ``handle`` can be looked up in the registry without crashing.

    :func:`_record_for` ends in a BARE ``_ISSUED.get(handle["handle_id"])``: a
    missing ``handle_id`` raises ``KeyError`` and an unhashable one (a caller
    passed a ``list``) raises ``TypeError``. Both are the SAME untrusted-input
    class F3 fixed in :func:`ensure_usable`, reaching the registry through the
    second call site -- :func:`is_expired`. Since ``is_expired`` is a boolean
    PREDICATE (its own contract says the typed refusal belongs to
    :func:`ensure_usable`, and a raising predicate would break any caller polling
    it), it cannot raise here; it uses this to fail closed instead. A handle
    that is not even a mapping, or whose ``handle_id`` is absent or unhashable,
    is not lookupable.
    """

    if not isinstance(handle, dict) or "handle_id" not in handle:
        return False
    try:
        hash(handle["handle_id"])
    except TypeError:
        return False
    return True


def is_expired(handle: DerivedHandle, *, now: float) -> bool:
    """True iff ``handle`` is at/past its expiry, decided from the TRUSTED record.

    Reads the RECORD's ``not_after`` (inclusive: ``now >= not_after``), never the
    handle's own copy. A handle with no issuance record is treated as expired
    (True) -- fail closed -- so a caller polling ``is_expired`` cannot get a
    "still live" answer for an unknown or post-restart handle. A MALFORMED handle
    (not a mapping, or a missing / unhashable ``handle_id`` that the registry
    lookup cannot even key on) is a WEAKER case than "unknown", so under the same
    fail-closed contract it is treated as expired (True) too -- rather than
    letting the bare ``_record_for`` lookup crash with ``KeyError`` / ``TypeError``.
    This is a boolean PREDICATE: it never raises. For the typed refusal (and the
    tamper / full-shape check), call :func:`ensure_usable`.
    """

    # Fail closed on a handle the registry cannot even look up (missing /
    # unhashable handle_id, or not a mapping): treat as expired, never crash.
    if not _handle_id_is_lookupable(handle):
        return True
    record = _record_for(handle)
    if record is None:
        return True
    return now >= record.not_after


def ensure_usable(handle: DerivedHandle, *, now: float) -> TrustedHandleView:
    """Refuse an unusable handle; return a TRUSTED, record-sourced view if usable.

    The single enforcement point, and it trusts ONLY the issuance record, never
    the handle's self-reported fields. In order:

    0. **Well-formed shape.** Because the handle's fields are UNTRUSTED, the very
       first step validates the COMPLETE shape -- ``handle`` is a mapping, every
       required key is present, and each value is of a type the enforcement can
       operate on (a ``handle_id`` that keys the registry, a ``scopes`` that
       ``set()`` accepts, a ``not_after`` that ``>=`` orders). A missing or
       wrong-typed field is the SAME class of untrusted input as a widened scope,
       so it is a typed refusal (:class:`HandleTamperedError`, ``auth``), not a
       bare ``KeyError`` / ``TypeError`` that would carry no ``error_class``,
       skip the audit, and jump the decision chain onto the caller.
    1. **Finite clock.** ``now`` MUST be finite. A ``NaN`` ``now`` makes every
       ``now >= not_after`` comparison False, which would let an EXPIRED handle
       through -- the expiry check silently disabled by the caller's clock. So a
       non-finite ``now`` is refused up front (:class:`HandleExpiredError`, typed
       ``input``); the enforcement never trusts the clock it was handed without
       checking it.
    2. **Issued here?** Look the handle up by ``handle_id`` in the process-local
       registry. If absent -> :class:`HandleNotIssuedError` (typed ``auth``):
       the fail-closed refusal for an unknown, forged, cross-process, or
       post-restart handle.
    3. **Untampered?** The presented handle must agree with the record on every
       axis a caller could otherwise abuse: ``scopes`` must be a subset of the
       record's; ``not_after`` must not be later; ``generation``,
       ``binding_fingerprint``, ``service_id`` and ``credential_mode`` must
       match exactly. Any disagreement -> :class:`HandleTamperedError` (typed
       ``auth``). A caller who widened the scope, pushed the expiry, flipped the
       target service, or swapped the credential mode on the dict is refused
       here, not obeyed.
    4. **Not expired?** Compare the (now-known-finite) ``now`` against the
       RECORD's ``not_after`` (inclusive). If expired ->
       :class:`HandleExpiredError` (typed ``input``).

    On success it returns a :class:`TrustedHandleView` built from the RECORD --
    not the handle. **A consumer MUST route on this returned view**
    (``service_id`` / ``credential_mode`` / ``scopes``) and MUST NOT read those
    off the mutable handle: the return value is the trusted answer, the handle is
    only a claim. The decision and the returned values never depend on a field
    the caller can edit.
    """

    # (0) The handle's fields are untrusted, so validate the COMPLETE shape
    # before reading ANY of them. A missing/wrong-typed field is refused as
    # tampered (typed) rather than crashing the first ``handle[...]`` read with
    # an uncaught KeyError/TypeError that would bypass the whole decision chain.
    _validate_handle_shape(handle)

    # (1) The clock itself must be finite; a NaN now would silently pass the
    # expiry comparison (NaN >= x is False) and let an expired handle through.
    if not math.isfinite(now):
        error = operation_error(
            "input",
            f"now must be a finite POSIX timestamp, got {now!r}",
        )
        raise HandleExpiredError(error)

    record = _record_for(handle)
    if record is None:
        error = operation_error(
            "auth",
            "handle is not recognized by this issuer "
            "(unknown, forged, from another process, or issued before a restart)",
        )
        raise HandleNotIssuedError(error)

    # The handle must not claim more authority than the record grants, and must
    # not differ on the routing/auth axes. These compare the caller-held copy
    # against the trusted record; a caller-side change is surfaced, not swallowed.
    if not set(handle["scopes"]).issubset(record.scopes):
        error = operation_error(
            "auth",
            "handle scope claim exceeds what was issued",
        )
        raise HandleTamperedError(error)
    if handle["not_after"] > record.not_after:
        error = operation_error(
            "auth",
            "handle expiry claim is later than what was issued",
        )
        raise HandleTamperedError(error)
    if handle["generation"] != record.generation:
        error = operation_error(
            "auth",
            "handle generation does not match the issued record",
        )
        raise HandleTamperedError(error)
    if handle["binding_fingerprint"] != record.binding_fingerprint:
        error = operation_error(
            "auth",
            "handle binding fingerprint does not match the issued record",
        )
        raise HandleTamperedError(error)
    if handle["service_id"] != record.service_id:
        error = operation_error(
            "auth",
            "handle service_id does not match the issued record",
        )
        raise HandleTamperedError(error)
    if handle["credential_mode"] != record.credential_mode:
        error = operation_error(
            "auth",
            "handle credential_mode does not match the issued record",
        )
        raise HandleTamperedError(error)

    # Expiry is decided from the RECORD, not the handle's own not_after.
    if now >= record.not_after:
        error = operation_error(
            "input",
            "handle expired: not_after " f"{record.not_after!r} <= now {now!r}",
        )
        raise HandleExpiredError(error)

    # Hand back the trusted view built from the RECORD. Callers route on THIS,
    # never on the mutable handle.
    return TrustedHandleView(
        handle_id=record.handle_id,
        service_id=record.service_id,
        credential_mode=record.credential_mode,
        scopes=tuple(sorted(record.scopes)),
        generation=record.generation,
        binding_fingerprint=record.binding_fingerprint,
        not_after=record.not_after,
    )
