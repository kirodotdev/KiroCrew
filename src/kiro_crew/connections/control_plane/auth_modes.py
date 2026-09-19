"""W01 · L05: per-operation permitted credential modes + a deny-by-default check.

Where :mod:`kiro_crew.connections.control_plane.operation` fixes the closed
credential-mode vocabulary (Axis B) and carries the ONE mode a descriptor
authenticates with, this module answers the adjacent question a dispatch asks at
call time: **is the credential mode the caller offers permitted for THIS
operation?** :func:`permit_operation` is the ONE authorization decision, and it
consults BOTH the operation's descriptor (its declared ``credential_modes``) and
the governance policy -- there is deliberately no public policy-only sibling that
skips the descriptor, because a function named like an authz decision that did
not check the descriptor would be a bypass waiting to be used.

Descriptor declares, policy narrows (real enforcement, not a docstring)
-----------------------------------------------------------------------
The L01 :class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor`
carries ``credential_modes`` -- the SET of modes the operation supports at all,
aligned one-to-one with the manifest's per-operation ``auth_modes`` array. That
declared set is the OUTER BOUND: a governance policy (this module's
:data:`PermittedModes` / :data:`PermittedModeRegistry`) may only SUBTRACT from
it, never add. :func:`permit_operation` (and its registry sibling
:func:`permit_registered_operation`) enforce that in RUNNING CODE by deciding
against the EFFECTIVE set -- :func:`effective_permitted_modes`, the intersection
``policy ∩ descriptor["credential_modes"] ∩ CREDENTIAL_MODES``. So a mode the
descriptor did not declare is DENIED **even if the policy tried to permit it**,
and an UNKNOWN mode (outside the L01 closed set) is DENIED **even if the
descriptor and the policy both carry it** -- the closed-set floor, which is not
redundant because a ``TypedDict`` descriptor and a ``frozenset`` policy are
neither validated at runtime, so a bogus string can sit in both and a bare
``policy ∩ declared`` would keep it. This is deliberate: the seam's ``context.py``
says the selected mode must come "from within the descriptor's declared set",
but that sentence is a ``TypedDict`` docstring and a ``TypedDict`` validates
NOTHING at runtime -- the intersection here is the code that actually makes it
true, and the slice's counter-example tests prove the denials rather than the
wording. The bare policy-membership predicate is kept as the PRIVATE
:func:`_is_mode_permitted` (underscore, absent from ``__all__``) so it cannot be
mistaken for authorization.

Pinned to the L01 closed set, no new words
-------------------------------------------
Every mode this module names is one of the three L01
:data:`~kiro_crew.connections.control_plane.operation.CredentialMode` values
(``oauth_user`` / ``fine_grained_pat`` / ``service_to_service``), which are
themselves copied verbatim from the manifest's per-operation ``auth_modes``
array (``connector-capability-manifest.md``). This slice neither invents a
fourth value nor widens the closed set: a permitted-modes declaration is a
SUBSET of :data:`~kiro_crew.connections.control_plane.operation.CREDENTIAL_MODES`,
validated as such, and a declaration naming anything outside that set is
rejected as a programming error rather than silently honoured.

Deny-by-default
---------------
An operation that declares NO permitted mode is DENIED, never allowed. The
absence of a declaration is not an open door: :func:`permit_operation` treats an
empty permitted set (and an operation missing from a
:data:`PermittedModeRegistry`) as "nothing is permitted here", so a new
operation someone forgot to configure fails closed instead of accepting every
credential. A deny returns the L01 typed error built with
:func:`~kiro_crew.connections.control_plane.errors.operation_error` (RUN-01
class ``auth``), so the error text goes through the same unconditional
redact-then-truncate discipline every other error surface in this subsystem
uses -- this module opens no un-redacted error channel of its own.

Zero credential values
-----------------------
The whole API accepts and returns only **mode identifiers**
(:data:`~kiro_crew.connections.control_plane.operation.CredentialMode`) and
**reference strings** (an ``operation_id``). It never accepts, stores, or
returns a token, a client secret, a bearer, or any credential plaintext -- the
same "references, never a value" invariant the sibling
:mod:`~kiro_crew.connections.control_plane.context` module carries. A permitted
mode says which KIND of credential is allowed; resolving a KIND to a live
credential is a later leaf in kiro-cli custody and is explicitly not here.

Two orthogonal axes, kept apart (the load-bearing invariant of this slice)
--------------------------------------------------------------------------
This repo has two things spelled "auth mode"; they are different axes and this
module operates on exactly ONE of them:

- **Axis A -- registration mode** answers "where did the OAuth *client* come
  from": ``dcr`` (dynamic client registration) vs ``preregistered``. It lives in
  :class:`kiro_crew.connections.registry.AuthConfig`, is read through
  :func:`kiro_crew.connections.registry.auth_mode` /
  :func:`kiro_crew.connections.registry.is_preregistered`, and its values are
  :data:`kiro_crew.connections.registry.AUTH_MODE_DCR` /
  :data:`~kiro_crew.connections.registry.AUTH_MODE_PREREGISTERED`. **This module
  does not import it, restate it, redefine it, or change any of its APIs.**
- **Axis B -- credential mode** answers "what credential does *this operation*
  authenticate its call with": ``oauth_user`` / ``fine_grained_pat`` /
  ``service_to_service``. That is the L01 axis this module gates on.

The two value sets are DISJOINT (``{dcr, preregistered}`` vs
``{oauth_user, fine_grained_pat, service_to_service}``) and neither axis is
derivable from the other -- a ``preregistered`` client (Axis A) can still
authenticate an operation as ``oauth_user`` or ``service_to_service`` (Axis B).
Inferring a permitted credential mode from a registration mode would reintroduce
exactly the collapse the manifest's two-axis design ("an account type never
stands in for an auth mode") exists to prevent. L01 already set the precedent of
naming Axis B ``credential_mode`` on the descriptor to keep the axes apart in
code; this module keeps the same discipline and a companion test asserts the two
value sets do not intersect.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional

from kiro_crew.connections.control_plane.errors import (
    OperationError,
    operation_error,
)
from kiro_crew.connections.control_plane.operation import (
    CREDENTIAL_MODES,
    CredentialMode,
    OperationDescriptor,
)

#: Bumped when this module's declaration/decision shape changes, mirroring the
#: module-level schema-version constant every sibling control-plane module
#: carries.
AUTH_MODES_SCHEMA_VERSION = 1

#: A per-operation declaration of which credential modes are permitted: an
#: immutable set of L01 :data:`CredentialMode` values. A ``frozenset`` (not a
#: mutable set) so a declaration handed to :func:`permit_operation` cannot be
#: mutated after it is authored, and the empty ``frozenset()`` is the explicit
#: "nothing permitted" declaration deny-by-default rests on.
PermittedModes = frozenset[CredentialMode]

#: A mapping from an ``operation_id`` reference to its permitted-mode set. An
#: operation absent from the mapping is DENIED (deny-by-default): the mapping is
#: not an allowlist-with-holes, it is the complete statement of what is
#: permitted, and anything unstated is nothing.
PermittedModeRegistry = Mapping[str, PermittedModes]


def declare_permitted_modes(modes: Iterable[CredentialMode]) -> PermittedModes:
    """Build a validated per-operation permitted-mode declaration.

    ``modes`` is the set of credential modes THIS operation permits -- a subset
    of the L01 closed set (:data:`CREDENTIAL_MODES`). The result is an immutable
    :data:`PermittedModes`. An empty iterable is a legal declaration: it means
    "no mode is permitted", which deny-by-default honours by denying every call.

    A value outside the L01 closed set is a programming error (a fourth,
    invented credential mode this slice must never mint), and is rejected with a
    ``ValueError`` naming the offending values rather than silently widening the
    closed set. This mirrors the manifest's own rule: a genuinely new credential
    mode is a scoped revision of the manifest's ``auth_modes`` vocabulary, never
    a value smuggled in through a declaration here.
    """

    declared = frozenset(modes)
    unknown = declared - frozenset(CREDENTIAL_MODES)
    if unknown:
        raise ValueError(
            "permitted modes outside the L01 credential-mode closed set: "
            + ", ".join(sorted(unknown))
        )
    return declared


def _is_mode_permitted(permitted: PermittedModes, offered: CredentialMode) -> bool:
    """Policy-only membership check -- **NOT an authorization decision.**

    A pure predicate over a policy set alone. It does NOT consult the operation
    descriptor's declared ``credential_modes``, so it must NOT decide
    whether a call is authorized: an operation's outer bound is the descriptor,
    and authorization is :func:`permit_operation`. This helper exists only as an
    internal building block; it is deliberately private (underscore) and absent
    from ``__all__`` so no caller can mistake a bare policy-membership test for
    an authz decision. ``offered`` is a mode IDENTIFIER, never a credential value.
    """

    return offered in permitted


def effective_permitted_modes(
    descriptor: OperationDescriptor,
    permitted: PermittedModes,
) -> PermittedModes:
    """Return the modes actually permitted: policy ∩ descriptor ∩ closed set.

    **This is the real enforcement of "descriptor declares, policy narrows".**
    The descriptor's ``credential_modes`` is the OUTER BOUND -- the complete
    statement of which modes the operation supports at all -- and a policy set
    may only SUBTRACT from it. The effective set is therefore the intersection
    ``permitted ∩ descriptor["credential_modes"] ∩ CREDENTIAL_MODES``: any mode a
    policy tried to permit that the descriptor did not declare is dropped, in
    running code, not merely disallowed by a docstring (a ``TypedDict`` validates
    NOTHING at runtime).

    The third factor -- intersecting with :data:`CREDENTIAL_MODES` -- is the
    closed-set floor, and it is NOT redundant with the other two. Both a
    descriptor's ``credential_modes`` and a policy set are ``TypedDict`` /
    ``frozenset`` values with no runtime validation, so an out-of-closed-set
    string (a typo, a fabricated fourth mode) can appear in BOTH; the plain
    ``permitted ∩ declared`` would then keep it (non-empty intersection == allow),
    silently violating "an unknown mode is always denied". Intersecting with the
    L01 closed set makes an unknown mode fall out no matter how many places carry
    it, so the deny-unknown rule holds structurally rather than by trusting the
    inputs to have been validated upstream.

    Pure and side-effect-free; returns an immutable :data:`PermittedModes`. All
    inputs are mode identifiers / references, no credential value.
    """

    declared = frozenset(descriptor["credential_modes"])
    return frozenset(permitted) & declared & frozenset(CREDENTIAL_MODES)


def permit_operation(
    descriptor: OperationDescriptor,
    offered_mode: CredentialMode,
    permitted: PermittedModes,
) -> Optional[OperationError]:
    """THE authorization decision: allow/deny against descriptor AND policy.

    This is the module's ONE authorization entrypoint. It decides against the
    EFFECTIVE set (:func:`effective_permitted_modes` --
    ``permitted ∩ descriptor["credential_modes"] ∩ CREDENTIAL_MODES``), so every
    rule holds in one place:

    - a mode the descriptor did NOT declare is DENIED **even if ``permitted``
      contains it** -- a policy cannot permit past the descriptor's outer bound
      (the counter-example the slice is judged on);
    - a mode the descriptor declared but the policy did not permit is DENIED
      (policy narrows within the bound);
    - an UNKNOWN mode -- outside :data:`CREDENTIAL_MODES` -- is DENIED even if the
      descriptor and the policy BOTH carry it (the closed-set floor);
    - deny-by-default: an empty ``permitted`` (or an empty declared set) denies
      every mode.

    There is deliberately no policy-only sibling that skips the descriptor: a
    public function named like an authorization decision that did not consult the
    descriptor would be a bypass waiting to be used, so the descriptor is
    REQUIRED here. Returns ``None`` on allow, or an L01 RUN-01 ``auth``
    :class:`~kiro_crew.connections.control_plane.errors.OperationError` on deny,
    built with :func:`~kiro_crew.connections.control_plane.errors.operation_error`
    so ``detail`` is unconditionally redacted-then-truncated. The detail names
    only the operation id and mode identifiers -- never a credential value -- and
    distinguishes the deny reasons (unknown / undeclared / unpermitted). The
    ``operation_id`` is read off the descriptor so both axes decide from one
    source of truth.
    """

    operation_id = descriptor["operation_id"]
    declared = frozenset(descriptor["credential_modes"])
    effective = effective_permitted_modes(descriptor, permitted)
    if offered_mode in effective:
        return None
    if offered_mode not in frozenset(CREDENTIAL_MODES):
        return operation_error(
            "auth",
            f"credential mode '{offered_mode}' is not a known credential mode "
            f"for operation '{operation_id}'; known modes: "
            + ", ".join(sorted(CREDENTIAL_MODES))
            + " -- an unknown mode is always denied",
        )
    if offered_mode not in declared:
        return operation_error(
            "auth",
            f"credential mode '{offered_mode}' is not DECLARED by operation "
            f"'{operation_id}'; declared modes: "
            + (", ".join(sorted(declared & frozenset(CREDENTIAL_MODES))) if declared else "(none)")
            + " -- a policy cannot permit a mode the descriptor did not declare",
        )
    return operation_error(
        "auth",
        f"credential mode '{offered_mode}' is declared by operation "
        f"'{operation_id}' but not permitted by policy; effective permitted "
        "modes: " + (", ".join(sorted(effective)) if effective else "(none)"),
    )


def permit_registered_operation(
    descriptor: OperationDescriptor,
    offered_mode: CredentialMode,
    registry: PermittedModeRegistry,
) -> Optional[OperationError]:
    """:func:`permit_operation` against a whole :data:`PermittedModeRegistry`.

    Looks the descriptor's ``operation_id`` up in ``registry`` and defers to
    :func:`permit_operation`, so the descriptor's declared set is the outer
    bound, the registry entry only narrows within it, and an unknown mode is
    denied by the closed-set floor. An operation MISSING from ``registry``
    resolves to the empty permitted set and is denied (deny-by-default). Returns
    ``None`` on allow, an ``auth`` typed error on deny. All arguments are
    identifiers / references; no credential value is accepted or returned.
    """

    permitted = registry.get(descriptor["operation_id"], frozenset())
    return permit_operation(descriptor, offered_mode, permitted)
