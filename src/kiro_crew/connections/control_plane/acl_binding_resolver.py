"""W01 · L04: the host-side adapter that satisfies the knowledge ACL's
``BindingResolver`` seam, backed by the trusted binding store.

WHY THIS FILE EXISTS
--------------------
The knowledge subsystem's ACL (``kiro_crew.knowledge.acl``) gates every managed
(cloud/structured) item behind a per-candidate identity check. It does NOT know
how a principal maps to a provider account -- that is a HOST/W01 concern -- so it
declares a seam and asks the host to fill it:

    class BindingResolver(Protocol):            # acl.py, @runtime_checkable
        def resolve(self, principal, provider, account) -> AccessContext | None: ...

    # "Implemented by the host/W01 layer, not here."  -- acl.py docstring

This module is that host implementation. It maps ONE candidate's
``(principal, provider, account)`` to the provider-mapped identity the principal
holds there, reading it from L04's trusted :class:`BindingStore`.

CALL-SHAPE CONFORMANCE, AND WHY THAT IS NOT THE WHOLE CONTRACT
--------------------------------------------------------------
``BindingResolver`` is a ``@runtime_checkable`` ``Protocol``. This adapter matches
its CALL SHAPE -- ``resolve(self, principal, provider, account)`` returning a
value or ``None`` -- and it does NOT import ``kiro_crew.knowledge.acl`` (that is a
knowledge-side module on an un-merged branch; importing it here would invert the
dependency and is not reachable from a control-plane wheel).

But matching the method signature is NOT the same as producing an object the ACL
can consume. The ACL's own gate reads a field this adapter does NOT produce:
``_subject_tenant_ok`` evaluates ``grant.subjects & ctx.subject_ids``, where
``ctx.subject_ids`` is a ``@property`` on ACL's ``AccessContext``
(``frozenset({subject, *groups})``). **W01 has no ``subject_ids`` anywhere** -- so
the :class:`AccessGrant` this adapter returns CANNOT be handed to the ACL gate
as-is; doing so would raise ``AttributeError`` at ``ctx.subject_ids``. Claiming
the grant is interchangeable with, or drop-in usable by, ACL's ``AccessContext``
would be exactly the kind of "documented property that does not exist" this stack
has been bitten by before, so this module does not claim it.

THE ACTUAL BRIDGE CHAIN
-----------------------
What W01 produces and what the ACL still has to do are two different halves:

1. W01 (this module) hands the ACL an :class:`AccessGrant` carrying the
   provider-mapped, VERIFIED identity for one candidate: ``subject`` and
   ``tenant`` from the resolved binding's ``subject_ref`` / ``tenant_ref``, and
   ``groups`` empty (W01 does not produce a group dimension).
2. The ACL side consumes commit ``dd715f12a`` via ordinary git and, ON THE ACL
   SIDE, bridges an :class:`AccessGrant` into its own ``AccessContext`` --
   constructing ``AccessContext(subject=grant.subject, tenant=grant.tenant,
   groups=grant.groups)`` (or equivalent), which is where the ``subject_ids``
   property comes into existence. ``subject_ids`` is an ACL-OWNED derivation, NOT
   a field W01 emits.

So :class:`AccessGrant` is a plainly-named W01 transport record, deliberately NOT
a re-implementation of ACL's ``AccessContext`` (we do not copy the ACL class, its
``__post_init__`` invariant, or its ``subject_ids`` property). The one thing W01
does NOT cover, and the ACL side MUST supply, is stated explicitly: the
``subject_ids`` derivation the gate reads.

Because the Protocol type is not importable here, this module cannot assert
``isinstance(adapter, BindingResolver)``; the tests pin the CALL shape directly
and, separately, pin the bridge-chain contract (what fields ``AccessGrant``
carries AND that ``subject_ids`` is NOT among them, i.e. is left to the ACL).

WHAT W01 PUTS IN EACH FIELD IT DOES EMIT
----------------------------------------
* ``subject`` / ``tenant`` <- the resolved binding's ``subject_ref`` /
  ``tenant_ref``. These were produced by the ``SubjectTenantVerifier`` at INSERT
  time and stored; :meth:`BindingStore.resolve_for_acl` returns the STORED values
  and takes no caller-claimed identity -- so the tenant is a VERIFIED provider
  tenant read from the trusted store, the "trusted tenant" discipline.
* ``groups`` <- ALWAYS empty (``frozenset()``). W01 does NOT produce or invent a
  group/role dimension: a binding authorizes ONE provider identity, not a set of
  team/org roles. Group membership is a separate authority (the provider's
  directory/graph); the ACL side owns it. Fabricating groups here would be an
  unverified privilege grant.
* ``bypass_acl`` <- ALWAYS ``False``. ``bypass_acl=True`` is the ACL's LOCAL
  single-user library context; a provider binding is a managed, cross-identity
  path, so W01 never sets it true. That context is minted ACL-side.

THE ``account`` -> ``deployment_id`` AXIS (a named seam, not a silent equation)
-------------------------------------------------------------------------------
The ACL keys the resolver on ``(provider, account)``, where ``account`` is the
VENDOR-side account/tenant/org an object lives in -- per acl.py, "a Graph tenant
id, a Salesforce org id, a Drive driveId, a GitHub org/login, a Slack workspace
id". L04's uniqueness domain instead keys on ``deployment_id`` =
"the PROVIDER-SIDE deployment that hosts the account (a GitHub Enterprise host, a
Salesforce org, a Graph tenant deployment)". These are DIFFERENT axes:

* a GitHub ``account`` (org/login) is NOT a GHE host -- two orgs on one GHE host
  would collapse to one "deployment" if account were used verbatim;
* a Graph ``account`` may be a ``driveId`` (a resource), not the tenant
  deployment; using it as ``deployment_id`` would also duplicate ``tenant_ref``,
  which already carries the verified tenant.

So this adapter does NOT pass ``deployment_id=account`` (the previous round did,
and that silently mislabelled a vendor account as a hosting deployment). Instead
it takes an explicit, injected ``account_to_deployment`` seam that a host wires
to translate ``(service_id, account)`` into the ``deployment_id`` that hosts it.
When no such mapping is provided, or it returns ``None``, this adapter FAILS
CLOSED (returns ``None``) rather than guess -- and the absence of a
first-class ``(service_id, account) -> deployment_id`` registry in W01 today is a
REAL, NAMED gap (see the report / the ``test_account_to_deployment_*`` tests),
not something to paper over by equating the two axes.

FAIL-CLOSED
-----------
Per the Protocol, ``resolve`` returns ``None`` -- deny this candidate -- for every
"no usable binding" case: an unmappable ``provider`` string, an
unverified/empty/local principal, an ``account`` this host cannot map to a
deployment, and a principal that holds no (or a revoked) binding on the resolved
(deployment, service). It raises only when the trusted store itself is unreadable
(corruption is surfaced, not silently read as "no binding").

PRINCIPAL TRUST CONTRACT (a named interface gap, not an implementation stopgap)
-------------------------------------------------------------------------------
An ACL ``QueryPrincipal`` carries a ``principal_id`` and ``local_library`` but NO
verification provenance -- nothing on it says whether that id was ESTABLISHED by
authentication or merely echoed from an inbound request. The ACL side has, at
least in one revision (``a31``), minted a session principal from a bare
``X-Session-Key`` fallback; a bare session key is not an authentication proof and
must NOT become an authorization subject. This adapter therefore does not trust
``principal_id`` on its face: it accepts a principal as a subject ONLY when a
wired ``principal_verified`` predicate vouches that its verification is
established, and FAILS CLOSED otherwise (it will not accept an unproven principal
to make the chain run). The predicate is the trust-contract seam the ACL/request
layer must supply -- the concrete fields it needs to expose are NAMED here so the
gap is on the table, not papered over: a verified-principal marker (or the signed
request context the id was derived from) that distinguishes an authenticated
caller id from an inbound ``X-Session-Key`` echo. Same discipline as
``tenant_ref`` / ``subject_ref``: only verifier-produced identity counts, never a
caller's self-report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import SERVICE_IDS, ServiceId

#: A host-provided translation from an ACL ``(service_id, account)`` -- the
#: VENDOR-side account/org/tenant/driveId a candidate lives in -- to the L04
#: ``deployment_id`` (the provider-side deployment that HOSTS that account). It
#: returns ``None`` when the host holds no deployment for that pair, which makes
#: the resolver fail closed. This is a seam, not a default: W01 has no built-in
#: ``(service_id, account) -> deployment_id`` registry yet (a named gap), so a
#: host that has not wired one gets fail-closed denials rather than a wrong axis.
AccountToDeployment = Callable[[ServiceId, str], Optional[str]]

#: Maps the ACL's ``provider`` connector-id (a free ``str`` on the ACL side, from
#: a candidate's ``ProviderResourceRef.provider``) to W01's CLOSED
#: :data:`ServiceId` set. The ACL uses ``'excel'`` for the Graph workbook
#: provider where W01's manifest range is ``'excel_shared_engine'``; every other
#: id the ACL lists ('sharepoint','onedrive','onenote','teams','outlook',
#: 'gmail','google_drive','salesforce','github','zoom','slack','asana') is a
#: verbatim member of :data:`ServiceId` and maps 1:1. ``'office_documents'`` has
#: no ACL provider id (it is a capability set, not a per-candidate provider) and
#: is intentionally absent from the VALUES here -- it is still a valid
#: ``ServiceId``, just never a ``provider`` the ACL asks about.
_PROVIDER_ALIASES: dict[str, ServiceId] = {
    "excel": "excel_shared_engine",
}


def map_provider_to_service_id(provider: str) -> ServiceId | None:
    """Map an ACL ``provider`` string to a :data:`ServiceId`, or ``None``.

    The mapping is over a CLOSED set: an exact :data:`ServiceId` member maps to
    itself, a known alias (``'excel' -> 'excel_shared_engine'``) is translated,
    and ANY other string -- an unknown, misspelled, or free-form provider --
    returns ``None`` (REFUSE). We do not leniently accept arbitrary strings: an
    unrecognised provider is a candidate this resolver cannot vouch for, so the
    ACL must deny it, exactly as it denies an unresolvable binding.
    """

    if provider in _PROVIDER_ALIASES:
        return _PROVIDER_ALIASES[provider]
    if provider in SERVICE_IDS:
        # provider is a verbatim ServiceId member.
        return provider  # type: ignore[return-value]
    return None


class _LocatorSpec:
    """The required locator keys for one provider, and which key is its ENDPOINT.

    ``required`` -- locator keys a well-formed ref for this provider MUST carry;
    a missing key REFUSES (no best-effort completion).
    ``endpoint_key`` -- the locator key that names the provider-side deployment
    endpoint/host (the discriminator ``(provider, account)`` lacks), or ``None``
    when this provider's real connector emits NO endpoint field yet.
    ``implemented`` -- True only where a real connector constructs this ref today
    (per the ACL ``ProviderResourceRef`` docstring); PROPOSED shapes are recorded
    for completeness but are NOT a claim they are wired.
    """

    __slots__ = ("required", "endpoint_key", "implemented")

    def __init__(self, required, endpoint_key, implemented):
        self.required = tuple(required)
        self.endpoint_key = endpoint_key
        self.implemented = implemented


#: Per-:data:`ServiceId` locator contract. Only three providers have a real code
#: source constructing the ref today (github / google_drive / salesforce, per
#: acl.py); the rest are PROPOSED (no connector emits them yet) and are marked so
#: -- their shapes are recorded from the ACL doc, NOT invented here.
#:
#: * salesforce carries a REAL endpoint (`instanceUrl`) -- the discriminator used
#:   for the same-account-on-two-deployments case.
#: * github's real connector emits {owner, repo, number|sha|check_run_id} and
#:   carries NO endpoint/host field yet -- so `endpoint_key` is None and the
#:   GitHub same-account-cross-host case stays fail-closed until the GitHub
#:   connector (W02, owner per the ACL doc) adds one. We do NOT fabricate it.
#: * google_drive's connector emits {fileId, [driveId]} -- no host endpoint.
#: * the Microsoft Graph family + gmail/zoom/slack/asana are PROPOSED; their
#:   endpoint-bearing shapes are unconfirmed, so they resolve fail-closed here.
_LOCATOR_SPECS: dict[ServiceId, _LocatorSpec] = {
    # IMPLEMENTED (a connector builds the ref):
    "salesforce": _LocatorSpec(
        required=("instanceUrl",), endpoint_key="instanceUrl", implemented=True
    ),
    "github": _LocatorSpec(required=("owner", "repo"), endpoint_key=None, implemented=True),
    "google_drive": _LocatorSpec(required=("fileId",), endpoint_key=None, implemented=True),
    # PROPOSED (no connector emits these yet; shapes from the ACL doc, unconfirmed).
    # endpoint_key is None because the shape is not wired -- resolution stays
    # fail-closed rather than trust an unbuilt/undocumented endpoint field.
    "sharepoint": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "onedrive": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "onenote": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "teams": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "outlook": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "excel_shared_engine": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "office_documents": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "gmail": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "zoom": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "slack": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
    "asana": _LocatorSpec(required=(), endpoint_key=None, implemented=False),
}


@dataclass(frozen=True)
class _MinimalRef:
    """The endpoint-less ref the thin ``resolve(provider, account)`` shell builds.

    It carries only provider + account and an empty locator, so it structurally
    matches what ``resolve_ref`` reads (``.provider`` / ``.account`` /
    ``.locator``) while carrying NO endpoint -- which is exactly why the older
    two-arg call resolves fail-closed for any endpoint-requiring provider.
    """

    provider: str
    account: str
    resource_id: str = ""
    locator: dict = field(default_factory=dict)


class StoreBackedAccountToDeployment:
    """The production account -> deployment mapping: reads the TRUSTED store.

    This is the real implementation the resolver defaults to -- NOT ``None`` and
    NOT a fixture lambda. Its source of truth is the persisted binding store: a
    binding was admitted with the vendor ``account`` it is for and the
    ``deployment_id`` that hosts it, so the pairing already lives in trusted,
    persistent state. It is a callable of the :data:`AccountToDeployment` shape,
    so it drops straight into the resolver's mapping slot.

    It delegates to :meth:`BindingStore.resolve_deployment_for_account`, which
    fails closed to ``None`` for an unknown ``(service_id, account)`` AND for the
    SAME account name hosted on more than one deployment -- the latter is
    unresolvable from ``(provider, account)`` alone (a NAMED interface gap; the
    ACL's ``resolve`` signature carries no endpoint/host discriminator, see the
    module docstring). It never guesses and never equates the account with a
    deployment.
    """

    def __init__(self, store: BindingStore) -> None:
        self._store = store

    def __call__(self, service_id: ServiceId, account: str) -> Optional[str]:
        return self._store.resolve_deployment_for_account(service_id=service_id, account=account)


@dataclass(frozen=True)
class AccessGrant:
    """A W01 transport record carrying one candidate's provider-mapped identity.

    This is NOT a re-implementation of ACL's ``AccessContext``: it deliberately
    carries only the fields W01 can VERIFY and produce, and it does NOT define
    ACL's ``subject_ids`` property or ``__post_init__`` invariant. The ACL side
    bridges this into its own ``AccessContext`` (see the module docstring's
    "bridge chain"), and that is where ``subject_ids`` is derived.

    * ``subject`` -- the verified provider subject id (the binding's
      ``subject_ref``), never a raw Kiro Crew session key or a caller-asserted
      value.
    * ``tenant`` -- the verified provider tenant/org boundary (the binding's
      ``tenant_ref``), read from the trusted store.
    * ``groups`` -- always empty for W01; a separate authority owns group
      membership, and the ACL folds it into ``subject_ids`` on its side.
    * ``bypass_acl`` -- always ``False`` for W01 (a provider binding is a managed,
      cross-identity path; the local-library bypass is an ACL-side context).

    NOT COVERED HERE (the ACL side must supply it when bridging): ``subject_ids``,
    the ``frozenset({subject, *groups})`` derivation the ACL gate reads.
    """

    subject: str
    tenant: str
    groups: frozenset[str] = field(default_factory=frozenset)
    bypass_acl: bool = False


class ControlPlaneBindingResolver:
    """Host-side ``BindingResolver`` implementation, over the trusted store.

    Matches the ACL ``BindingResolver`` CALL shape --
    ``resolve(self, principal, provider, account)`` returning an
    :class:`AccessGrant` or ``None`` -- without importing the ACL module. The
    returned :class:`AccessGrant` is bridged into ACL's ``AccessContext`` on the
    ACL side (see the module docstring); the ACL gate cannot read it as-is, since
    it reads a ``subject_ids`` this adapter does not produce.

    Construct it with the :class:`BindingStore` that holds this host's admitted
    bindings. By DEFAULT the account -> deployment mapping is the trusted,
    store-backed :class:`StoreBackedAccountToDeployment` (reads the persisted
    store, not a caller map and not a fixture lambda); an alternate
    ``account_to_deployment`` may be injected for tests, but the default is a real
    implementation, never ``None``. The resolver adds no state of its own and
    never mutates the store.

    Principal trust: the adapter treats a principal as an authorization subject
    ONLY when its verification is ESTABLISHED. A ``QueryPrincipal`` carries a
    ``principal_id`` and ``local_library`` but NO verification provenance, so a
    principal minted from a bare ``X-Session-Key`` fallback (ACL ``a31``) is
    indistinguishable from a verified one by shape alone. A bare session key is
    not an authentication proof, so the adapter requires an explicit
    ``principal_verified`` predicate to vouch for the principal; with none wired,
    it FAILS CLOSED (it does not accept an unproven principal just to make the
    chain run). The verification predicate is the trust-contract seam the ACL
    side must supply (see the module docstring's trust-contract note).
    """

    def __init__(
        self,
        store: BindingStore,
        *,
        account_to_deployment: AccountToDeployment | None = None,
        principal_verified: Callable[[object], bool] | None = None,
    ) -> None:
        self._store = store
        # Production default: the trusted store-backed mapping, NOT None. An
        # injected seam (tests) overrides it; the default is a real implementation.
        self._account_to_deployment: AccountToDeployment = (
            account_to_deployment
            if account_to_deployment is not None
            else StoreBackedAccountToDeployment(store)
        )
        # Trust-contract seam: vouches that a principal's verification is
        # established. None => the trust contract is not wired, so no principal
        # can be accepted (fail closed) -- a bare session-key principal must never
        # be treated as verified just because it has a principal_id.
        self._principal_verified = principal_verified

    def resolve_ref(self, principal: object, ref: object) -> AccessGrant | None:
        """Map one candidate's full ``ProviderResourceRef`` to an identity.

        THIS is the single judgment path. ``ref`` is the ACL's four-field
        ``ProviderResourceRef``, consumed STRUCTURALLY (``.provider`` / ``.account``
        / ``.resource_id`` / ``.locator``) -- no import of the ACL type. It carries
        the endpoint/host discriminator the bare ``(provider, account)`` pair
        lacks, which is what lets the SAME account name on two provider
        deployments resolve correctly.

        Five FAIL-CLOSED checks, in order (any failure -> ``None``):

        1. **principal established** -- ``principal_id`` present, not
           ``local_library``, and VOUCHED by the wired ``principal_verified``
           predicate (no predicate wired => deny; a bare ``X-Session-Key`` echo is
           not proof);
        2. **provider maps to a :data:`ServiceId`** closed-set member, else deny;
        3. **required locator keys present** for that service (from
           :data:`_LOCATOR_SPECS`), no best-effort completion -- a missing key
           denies; a provider whose real connector emits no endpoint field yet
           (github today) or whose shape is only PROPOSED cannot yield an endpoint,
           so it denies here rather than guess;
        4. **endpoint is REGISTERED in the trusted store** -- the store is the
           sole authority on which deployments/endpoints exist. The endpoint read
           from the ref's locator is used only as a LOOKUP KEY into the store;
           ``resolve_deployment_for_account_endpoint`` returns a deployment only
           when a stored binding registered that exact ``(service, account,
           endpoint)``. A caller-supplied endpoint the store never registered does
           not match -> deny;
        5. **unambiguous** -- more than one deployment on the triple denies.

        The returned :class:`AccessGrant` view comes ENTIRELY from the store's
        resolved binding (VERIFIED ``subject_ref`` / ``tenant_ref``); the ``ref``
        is a lookup key ONLY and is never an identity source.
        """

        # (1) principal must be established -- the trust boundary, checked first.
        principal_id = getattr(principal, "principal_id", None)
        if not principal_id:
            return None
        if getattr(principal, "local_library", False):
            return None
        if self._principal_verified is None or not self._principal_verified(principal):
            return None

        provider = getattr(ref, "provider", None)
        account = getattr(ref, "account", None)
        locator = getattr(ref, "locator", None)
        if not isinstance(provider, str) or not isinstance(account, str) or not account:
            return None
        if locator is None:
            locator = {}

        # (2) provider -> ServiceId closed set.
        service_id = map_provider_to_service_id(provider)
        if service_id is None:
            return None

        spec = _LOCATOR_SPECS.get(service_id)
        if spec is None:
            return None

        # (3) required locator keys present (no best-effort completion).
        for key in spec.required:
            val = locator.get(key) if hasattr(locator, "get") else None
            if not isinstance(val, str) or not val:
                return None

        # A provider with no endpoint discriminator (github today, or a PROPOSED
        # shape) cannot be resolved on the endpoint axis -> fail closed. We do NOT
        # fabricate an endpoint for it.
        if spec.endpoint_key is None:
            return None
        endpoint = locator.get(spec.endpoint_key) if hasattr(locator, "get") else None
        if not isinstance(endpoint, str) or not endpoint:
            return None

        # (4)+(5) endpoint must be REGISTERED in the store; resolution keys on
        # (provider, account, endpoint); ambiguity denies. The store is the sole
        # authority -- an unregistered endpoint simply does not match.
        deployment_id = self._store.resolve_deployment_for_account_endpoint(
            service_id=service_id, account=account, endpoint=endpoint
        )
        if not deployment_id:
            return None

        binding = self._store.resolve_for_acl(
            kiro_principal=principal_id,
            deployment_id=deployment_id,
            service_id=service_id,
        )
        if binding is None:
            return None

        # The view is entirely the store's VERIFIED record; ref was a key only.
        return AccessGrant(
            subject=binding["subject_ref"],
            tenant=binding["tenant_ref"],
            groups=frozenset(),
            bypass_acl=False,
        )

    def resolve(self, principal: object, provider: str, account: str) -> AccessGrant | None:
        """Thin back-compat shell over :meth:`resolve_ref`.

        The older ``(provider, account)`` call shape carries NO endpoint, so it
        constructs a minimal ref (provider + account, empty locator) and defers to
        :meth:`resolve_ref` -- it is NOT a second judgment path. Because that
        minimal ref has no endpoint, a provider that requires an endpoint
        discriminator (every one with a registered endpoint) resolves fail-closed
        through this shell: an endpoint-bearing candidate MUST come in through
        ``resolve_ref`` with its full ref. This preserves the bridge's older
        fallback shape without letting it bypass the endpoint check.
        """

        return self.resolve_ref(principal, _MinimalRef(provider=provider, account=account))
