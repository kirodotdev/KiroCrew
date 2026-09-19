"""W01 · L02: AUTH-01, the authorized-account binding record.

L01 (:mod:`kiro_crew.connections.control_plane.context`) fixed that every
per-call reference is a REFERENCE, never a value, and named the one an operation
is dispatched under: ``binding_ref``. This module is what a ``binding_ref``
points AT -- the record that says "this binding names a *verified* subject
acting within a *verified* tenant, authenticated by a credential whose reference
(never value) is recorded here". It is still pure types with zero IO: it MINTS
and INCREMENTS a record, it does not persist one, resolve one to a credential,
or reach kiro-cli.

Four load-bearing properties, each a distinct defence:

1. **The binding id is random, not derived.** :func:`create_binding` mints
   ``binding_id`` from :func:`secrets.token_hex` -- unguessable, and NOT a
   function of the provider slug, the tenant, or the subject. A derived id would
   let anyone who knew the (often public) slug/tenant reconstruct or enumerate
   binding ids, turning a reference meant to be capability-like into a guessable
   handle. The id carries no meaning: it is a name, resolved through a table a
   later leaf owns, never parsed.

2. **Only a VERIFIED subject and tenant are recorded.** A binding must not store
   whatever identity the caller *claimed*. :func:`create_binding` takes a
   :class:`SubjectTenantVerifier` and stores ONLY what it returns; if
   verification fails the verifier raises :class:`BindingVerificationError` and
   NO binding is built. There is no path from a caller-asserted subject/tenant
   to a stored one that skips the verifier -- the verifier is a required
   argument, not an optional hook.

3. **``generation`` is a monotonic counter, and only that here.** A fresh
   binding is minted at :data:`INITIAL_GENERATION`; :func:`next_generation`
   returns a NEW record with ``generation`` incremented by one and every other
   field carried through unchanged. Its PURPOSE is L04's revoke fencing -- a
   revoke will raise the generation so anything stamped with an older one is
   fenced off -- but **this slice lands the field and the increment SEMANTICS
   only. It does not implement refresh or revoke** (that is L04), and nothing
   here decides what a bumped generation invalidates.

4. **The secret is a REFERENCE and metadata, never a value -- and the reference
   is PER-BINDING, not per-provider.** A binding records a :class:`SecretRef` --
   the vault entry NAME plus enough metadata (backend, the moment it was bound)
   to find and reason about the secret -- and it is a *hard invariant of the
   type* that no plaintext lives on it. Crucially the name is scoped to THIS
   binding's own verified identity (its ``binding_id`` and verified
   subject/tenant) via :func:`binding_scoped_secret_ref`, NOT merely to the
   provider slug: two bindings for two different subjects/tenants under one
   provider therefore point at two DIFFERENT vault entries, so a credential is
   bound to the identity it serves and one binding can never resolve to
   another's secret. The name still follows the ``CONNECTIONS_<SLUG>_...`` family
   ``oauth_clients.client_secret_name`` uses for the Secrets panel, with a
   per-binding scope segment appended. The legacy slug-level
   :func:`binding_secret_ref` is retained for compatibility but is explicitly
   NOT an identity-bound reference (see its docstring). Resolving that name to a
   :class:`~kiro_crew.secrets.SecretVault` / :class:`~kiro_crew.secrets.SecretValue`
   is a later leaf's job; this module only writes down WHERE the secret is.

Resolving a principal to its binding, on the verified identity alone
--------------------------------------------------------------------
:func:`resolve_binding_for_principal` answers "which binding does THIS principal
own" -- and it keys the lookup on a VERIFIED identity, never on what the caller
claimed. It runs the same :class:`SubjectTenantVerifier` contract
:func:`create_binding` uses, then matches a binding by the verifier's returned
``subject_ref``/``tenant_ref`` (plus ``service_id``). A claim that does not
verify raises :class:`BindingVerificationError`; a verified principal with no
matching binding (or, fail-closed, more than one) raises
:class:`BindingResolutionError`. Because both the lookup key and each binding's
``secret_ref`` are per-identity, principal B can never resolve to principal A's
binding or A's secret reference.


The old-custody invariant, restated because it is the sharpest edge
-------------------------------------------------------------------
Under the pre-native custody model the OAuth token chain lives entirely inside
kiro-cli and the Kiro Crew backend only ``stat()``s to probe whether a grant
exists. **This module never reads, copies, or references an old kiro-cli OAuth
token.** A ``SecretRef`` names a NEW authorization grant's secret under a NEW
trusted owner -- it is not a place to smuggle a moved-over legacy token, and the
type deliberately carries only a vault-entry name, not a filesystem path into
kiro-cli's token store.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Iterable, Protocol, TypedDict

from kiro_crew.connections.control_plane.operation import CredentialMode, ServiceId

#: Bumped when this record's shape changes, mirroring the sibling control-plane
#: modules (``context`` / ``result`` / ``errors`` / ``operation``).
BINDING_SCHEMA_VERSION = 1

#: Bytes of randomness behind ``binding_id``. 16 bytes = 128 bits = a 32-char
#: hex string -- the same unguessable-handle strength ``webhooks``/``deploy`` use
#: for their random ids, and far past any enumeration budget.
_BINDING_ID_BYTES = 16

#: The generation a freshly-minted binding starts at. A revoke (L04, NOT here)
#: raises it; this slice only lands the field and its increment semantics.
INITIAL_GENERATION = 1

#: Vault-name family for a binding's secret, mirroring
#: ``oauth_clients._SECRET_NAME_PREFIX`` so a binding secret and a pre-registered
#: client secret sort together in the Secrets panel rather than under two
#: schemes. The suffix distinguishes a per-binding grant secret from the
#: operator's ``_CLIENT_SECRET`` application credential.
_SECRET_NAME_PREFIX = "CONNECTIONS_"
_SECRET_NAME_SUFFIX = "_BINDING_SECRET"

#: Infix marking the per-binding scope segment inside a scoped secret name,
#: e.g. ``CONNECTIONS_GITHUB_BINDING_<scope>_SECRET``. Lets a reader tell a
#: per-binding (identity-scoped) name apart from the legacy slug-level one at a
#: glance, and keeps the scope hash in its own delimited field.
_SECRET_SCOPE_INFIX = "_BINDING_"

#: Hex chars of the per-binding scope segment. Derived from a SHA-256 over the
#: binding's identity (id + verified subject/tenant); 16 hex chars = 64 bits is
#: ample to keep two distinct identities from colliding on one vault name while
#: keeping the entry name readable. The scope is an OPAQUE disambiguator, not a
#: secret and not something to parse back into an identity.
_SECRET_SCOPE_HEX = 16

#: The vault backend a ``SecretRef`` names, matching ``SecretVault._BACKEND``.
#: A binding records WHICH backend holds the secret so a later resolver does not
#: have to assume; today there is one, and pinning it keeps the record honest if
#: a second is ever added.
SECRET_BACKEND_VAULT = "vault"


def _slug_token(slug: str) -> str:
    """Upper-case, hyphen-to-underscore, matching ``oauth_clients._slug_token``.

    Kept in step with the pre-registered-client naming so the two secret
    families share one spelling of a slug rather than drifting.
    """

    return slug.upper().replace("-", "_")


def binding_secret_ref(slug: str, *, backend: str = SECRET_BACKEND_VAULT) -> "SecretRef":
    """LEGACY slug-level secret name -- NOT an identity-bound reference.

    Returns a vault name derived ONLY from the provider ``slug``
    (``CONNECTIONS_<SLUG>_BINDING_SECRET``), so EVERY binding under one provider
    gets the SAME name. That is exactly why it must NOT be used as the secret
    reference a binding stores: two bindings for two different subjects/tenants
    would then share one credential, and "the credential is bound to the identity
    it serves" would not hold. This function is retained only for backward
    compatibility with call sites that still key a secret by slug alone (e.g. a
    single operator-level application credential per provider); a per-binding
    grant secret MUST use :func:`binding_scoped_secret_ref` instead, and
    :func:`create_binding` does.

    ``bound_at`` is stamped at call time. This returns only a NAME and metadata,
    never a value.
    """

    return {
        "name": f"{_SECRET_NAME_PREFIX}{_slug_token(slug)}{_SECRET_NAME_SUFFIX}",
        "backend": backend,
        "bound_at": time.time(),
    }


def _secret_scope(binding_id: str, subject_ref: str, tenant_ref: str) -> str:
    """Opaque per-binding scope segment from the binding's own identity.

    A SHA-256 over the binding id and its VERIFIED subject/tenant, truncated to
    :data:`_SECRET_SCOPE_HEX` hex chars. Two different identities yield different
    scopes, so their secret names differ; the same identity yields a stable
    scope. NUL-delimited so distinct field boundaries cannot be forged by a value
    that contains the delimiter. The result is an OPAQUE disambiguator, never
    reversed back into an identity and never itself a secret.
    """

    material = f"{binding_id}\x00{subject_ref}\x00{tenant_ref}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:_SECRET_SCOPE_HEX]


def binding_scoped_secret_ref(
    slug: str,
    *,
    binding_id: str,
    subject_ref: str,
    tenant_ref: str,
    backend: str = SECRET_BACKEND_VAULT,
) -> "SecretRef":
    """Build the PER-BINDING :class:`SecretRef` a binding stores.

    Unlike the legacy :func:`binding_secret_ref`, the vault name here is scoped
    to THIS binding's own verified identity: ``binding_id`` plus the verified
    ``subject_ref`` / ``tenant_ref`` are hashed into an opaque scope segment
    (:func:`_secret_scope`) placed inside the ``CONNECTIONS_<SLUG>_BINDING_...``
    family -- ``CONNECTIONS_<SLUG>_BINDING_<scope>_SECRET``. Two bindings for two
    different subjects/tenants under one provider therefore get two DIFFERENT
    names, so a credential is bound to the identity it serves and one binding can
    never point at another's secret.

    ``bound_at`` is stamped at call time. This returns only a NAME and metadata,
    never a value; the secret itself lives in and is read from the vault by a
    later leaf, under this per-binding name.
    """

    scope = _secret_scope(binding_id, subject_ref, tenant_ref)
    return {
        "name": f"{_SECRET_NAME_PREFIX}{_slug_token(slug)}{_SECRET_SCOPE_INFIX}{scope}_SECRET",
        "backend": backend,
        "bound_at": time.time(),
    }


class SecretRef(TypedDict):
    """A reference to a binding's secret -- its location and metadata, no value.

    Every field present, matching the sibling descriptors' shape.

    ``name`` -- the vault entry name. For a binding's own grant secret this is
    the PER-BINDING name ``CONNECTIONS_<SLUG>_BINDING_<scope>_SECRET`` from
    :func:`binding_scoped_secret_ref` (the scope segment is derived from the
    binding's identity, so two identities never share a name); resolved to a
    :class:`~kiro_crew.secrets.SecretValue` by a later leaf, never here.
    ``backend`` -- which store holds it (see :data:`SECRET_BACKEND_VAULT`).
    ``bound_at`` -- absolute POSIX-seconds UTC when the reference was recorded.

    It is a hard invariant that NO plaintext secret is ever placed on this type:
    the value stays in the vault and only its name travels in a binding.
    """

    name: str
    backend: str
    bound_at: float


class VerifiedIdentity(TypedDict):
    """The result of verifying a claimed subject + tenant -- both references.

    Returned by a :class:`SubjectTenantVerifier` and the ONLY source of the
    ``subject_ref`` / ``tenant_ref`` a binding records. Both are references in
    exactly the L01 sense (``context.OperationContext``): a subject/tenant NAME,
    never a credential value.

    ``subject_ref`` -- the verified subject (user/principal). ``tenant_ref`` --
    the verified tenant the subject was verified WITHIN.
    """

    subject_ref: str
    tenant_ref: str


class Binding(TypedDict):
    """An authorized-account binding: AUTH-01's record (W01 · L02).

    Every field present, matching the sibling control-plane descriptors' shape.
    A ``binding_ref`` (``context.OperationContext``) names one of these records.

    ``binding_id`` -- an unguessable random id (see :func:`create_binding`); NOT
    derived from slug/tenant/subject. ``service_id`` -- the neutral service range
    (L01 closed set). ``subject_ref`` / ``tenant_ref`` -- the VERIFIED identity
    (see :class:`VerifiedIdentity`), never the caller's raw claim.
    ``credential_mode`` -- Axis B: what credential this binding authenticates with
    (L01 closed set). ``generation`` -- a monotonic counter for L04 revoke fencing
    (field + increment only in this slice). ``secret_ref`` -- a reference to the
    secret, carrying its location and metadata but NEVER its value.
    ``created_at`` -- absolute POSIX-seconds UTC when the binding was minted.
    """

    binding_id: str
    service_id: ServiceId
    subject_ref: str
    tenant_ref: str
    credential_mode: CredentialMode
    generation: int
    secret_ref: SecretRef
    created_at: float


class SubjectTenantVerifier(Protocol):
    """Verifies a CLAIMED subject + tenant and returns the VERIFIED identity.

    :func:`create_binding` stores only what an implementation of this protocol
    returns, so a binding can never record an unverified, caller-asserted
    identity. An implementation MUST raise :class:`BindingVerificationError` when
    the claim does not check out; returning a :class:`VerifiedIdentity` is the
    sole signal that verification passed. Implementations live in later leaves /
    provider streams (they do the actual IO against a provider or a directory);
    this seam only fixes the contract they satisfy.
    """

    def __call__(
        self, *, claimed_subject: str, claimed_tenant: str, service_id: ServiceId
    ) -> VerifiedIdentity:  # pragma: no cover - Protocol signature, not a body
        ...


class BindingVerificationError(Exception):
    """Raised when a claimed subject/tenant fails verification.

    The one way :func:`create_binding` refuses to build a binding: a verifier
    raises this and no record is minted. The message is a short human-readable
    reason and MUST NOT carry a credential value -- a binding never touches a
    secret value, and neither does its failure path.
    """


class BindingResolutionError(Exception):
    """Raised when a VERIFIED principal resolves to no single binding.

    The typed refusal of :func:`resolve_binding_for_principal`: the principal
    verified, but the binding store holds either zero matches (nothing to
    resolve) or -- fail-closed -- more than one (an ambiguous store must never
    be silently narrowed to a guess). Distinct from
    :class:`BindingVerificationError`, which fires earlier when the CLAIM itself
    does not verify. The message names neither a secret value nor which other
    principals exist.
    """


def create_binding(
    *,
    service_id: ServiceId,
    claimed_subject: str,
    claimed_tenant: str,
    credential_mode: CredentialMode,
    verifier: SubjectTenantVerifier,
    slug: str,
    secret_backend: str = SECRET_BACKEND_VAULT,
) -> Binding:
    """Mint a :class:`Binding` for a VERIFIED subject/tenant, or refuse.

    The single sanctioned constructor. In order:

    1. Runs ``verifier`` over the CLAIMED subject/tenant. The subject/tenant that
       land on the binding are the verifier's returned :class:`VerifiedIdentity`,
       never the raw ``claimed_*`` arguments. A failed verification raises
       :class:`BindingVerificationError` (from the verifier) and this function
       does not build anything -- there is no partial or unverified binding.
    2. Mints a random, unguessable ``binding_id`` from :func:`secrets.token_hex`
       -- independent of slug/tenant/subject, so it cannot be reconstructed from
       public inputs.
    3. Records a PER-BINDING :class:`SecretRef` (name + metadata via
       :func:`binding_scoped_secret_ref`) scoped to this binding's own id and
       verified subject/tenant -- the secret's LOCATION, never its value, and
       distinct from every other binding's under the same provider.
    4. Stamps ``generation`` at :data:`INITIAL_GENERATION`; raising it later is
       :func:`next_generation` / L04, not this call.

    ``slug`` names the provider whose vault-secret family the ref belongs to
    (kept distinct from ``service_id``, the neutral range, exactly as the two are
    distinct in the registry). Raises :class:`BindingVerificationError` if the
    verifier rejects the claim.
    """

    # Verify FIRST, and let a rejection propagate before anything is minted: a
    # binding that failed verification must not exist even partially.
    verified = verifier(
        claimed_subject=claimed_subject,
        claimed_tenant=claimed_tenant,
        service_id=service_id,
    )

    binding_id = secrets.token_hex(_BINDING_ID_BYTES)
    return {
        "binding_id": binding_id,
        "service_id": service_id,
        # Store ONLY the verified identity -- never the claimed_* inputs.
        "subject_ref": verified["subject_ref"],
        "tenant_ref": verified["tenant_ref"],
        "credential_mode": credential_mode,
        "generation": INITIAL_GENERATION,
        # Per-binding: scoped to THIS binding's id + verified identity, so two
        # bindings under one provider never share a secret reference.
        "secret_ref": binding_scoped_secret_ref(
            slug,
            binding_id=binding_id,
            subject_ref=verified["subject_ref"],
            tenant_ref=verified["tenant_ref"],
            backend=secret_backend,
        ),
        "created_at": time.time(),
    }


def resolve_binding_for_principal(
    bindings: Iterable[Binding],
    *,
    service_id: ServiceId,
    claimed_subject: str,
    claimed_tenant: str,
    verifier: SubjectTenantVerifier,
) -> Binding:
    """Resolve the binding a VERIFIED principal owns -- on identity, not a claim.

    This is the trusted ``principal -> provider/account binding`` lookup. It does
    NOT trust the caller's asserted principal: it runs the SAME
    :class:`SubjectTenantVerifier` contract :func:`create_binding` uses over the
    ``claimed_*`` inputs, and then matches a binding by the verifier's returned
    :class:`VerifiedIdentity` (``subject_ref`` / ``tenant_ref``) together with
    ``service_id``. The claimed values are used ONLY to verify; the match key is
    the verified identity, so a caller cannot resolve to a binding by naming a
    subject/tenant it has not proven.

    Pure and IO-free: ``bindings`` is the candidate set the caller already holds
    (a later leaf owns where it is persisted and read from). Returns the single
    matching :class:`Binding`.

    Raises :class:`BindingVerificationError` if the claim does not verify (from
    the verifier). Raises :class:`BindingResolutionError`, fail-closed, if the
    verified principal matches no binding, or -- deliberately -- more than one:
    an ambiguous store is never silently narrowed to one guess.

    Because a binding's ``secret_ref`` is per-binding (see
    :func:`binding_scoped_secret_ref`), resolving principal B yields B's binding
    and thus B's own secret reference; it can never surface principal A's binding
    or A's secret.
    """

    verified = verifier(
        claimed_subject=claimed_subject,
        claimed_tenant=claimed_tenant,
        service_id=service_id,
    )
    subject_ref = verified["subject_ref"]
    tenant_ref = verified["tenant_ref"]

    matches = [
        b
        for b in bindings
        if b["service_id"] == service_id
        and b["subject_ref"] == subject_ref
        and b["tenant_ref"] == tenant_ref
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise BindingResolutionError(
            f"no binding for the verified principal on service {service_id!r}"
        )
    raise BindingResolutionError(
        f"ambiguous: {len(matches)} bindings match the verified principal on "
        f"service {service_id!r}"
    )


def next_generation(binding: Binding) -> Binding:
    """Return a NEW binding with ``generation`` incremented by one.

    Pure and total: every other field is carried through unchanged, including
    the verified identity, the random id, and the secret reference. This lands
    the INCREMENT SEMANTICS the generation counter exists for; it does NOT
    perform a revoke or a refresh, and it does NOT decide what an older
    generation is fenced out of -- that is L04. The input is not mutated (a
    shallow copy is returned), so a caller holding the prior record still sees
    the prior generation.
    """

    bumped: Binding = dict(binding)  # type: ignore[assignment]
    bumped["generation"] = binding["generation"] + 1
    return bumped
