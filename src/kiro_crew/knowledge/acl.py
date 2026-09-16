"""Query-time access control for the shared Knowledge Library.

The retriever's ``source_id``/``namespace`` filters are relevance labels, NOT a
security boundary (their own docstrings say so, and the graph leg ignores them
entirely). A knowledge base that ingests *shared* sources under an admin/service
identity must therefore gate what any given querying user actually sees at
RETRIEVAL time against that user's OWN permission -- never the ingestion
identity's, never a filter the caller could widen, and never a permission
snapshot taken at ingest time (see KB-01/ACL-05/ACL-09 in the connector
production stack's shared contracts).

Two design corrections drive this module, both from Root's re-read of an earlier
version that got them wrong:

1. THE BYPASS IS ITEM-SCOPED, NOT CALL-SURFACE-SCOPED. One ``KnowledgeStore``
   mixes trusted-local material (a personal folder, an Obsidian vault, pasted
   documents) with MANAGED cloud/structured items (SharePoint, OneDrive,
   Salesforce, a structured GitHub source). A path being *named* "personal
   dashboard" does not prove every candidate it retrieves is un-ACL'd local
   material. So a caller with no cross-identity boundary (the local single-user
   library) may see trusted-local items WITHOUT a grant, but a managed item is
   ALWAYS gated against a real current subject/tenant -- and when that caller
   carries no verifiable identity to check it against, the managed item is
   DENIED (that item, not the whole library). :class:`is_managed_source_type`
   draws the line; :class:`SubjectTenantAclPolicy` enforces it per item.

2. A STATIC INGEST-TIME GRANT IS NOT PROOF THE PROVIDER STILL GRANTS ACCESS. The
   ingest-written ``subjects`` snapshot and a locally-bumped ``acl_version``
   cannot, on their own, observe a revocation the provider made after ingest.
   Real query-time ACL therefore requires a REVALIDATION chain: a managed item's
   grant must be confirmed current within a staleness window by a trusted
   :class:`RevalidationHook` (the provider live-permission interface). That hook
   is a named DEPENDENCY, not yet implemented here; until it is wired, a managed
   item's grant is treated as UNVERIFIABLE and DENIED (fail-closed). A static
   grant that is never refreshed must never masquerade as a live check.

This module owns the DECISION over grant records the store holds; it performs no
provider I/O itself. The revalidation hook, when supplied, is where that I/O
lives.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# The public-visibility sentinel a grant record uses to mark an item every
# authenticated subject in its tenant may see (e.g. a public repo, a
# world-readable folder). It is spelled with characters a real subject id cannot
# contain so it can never collide with one. An item with NO grant record is NOT
# this -- absence is deny, and this is an explicit, ingest-written allow.
PUBLIC_SUBJECT = "<public>"

# The tenant sentinel meaning "belongs to no specific tenant / cross-tenant
# public". A grant record carrying it is visible to any authenticated context
# regardless of that context's tenant, PROVIDED the subject test also passes
# (i.e. it is normally paired with PUBLIC_SUBJECT). It exists so genuinely
# tenant-agnostic public content is expressible without weakening the
# same-email-different-tenant rule (ACL-06) for everything else.
PUBLIC_TENANT = "<public-tenant>"

# The source ``trust_class`` provenance values (mirrored from
# knowledge/store.py, kept here so acl.py has no store import). A source is
# TRUSTED-LOCAL only with the explicit ``local_admitted`` stamp its local
# creator wrote; everything else -- the ``managed`` stamp, an unstamped/unknown
# source, or a missing source row -- is MANAGED and gated. This is the
# evidence-based provenance axis, NOT a source_type name guess.
TRUST_LOCAL = "local_admitted"
TRUST_MANAGED = "managed"


def is_managed_trust_class(has_source: bool, trust_class: str | None) -> bool:
    """True when an item must be treated as managed (gated + revalidated).

    TRUSTED-LOCAL requires POSITIVE provenance evidence: a source explicitly
    stamped :data:`TRUST_LOCAL` by a trusted local creator. Everything else is
    managed (fail-closed) -- Root's decision: "no source, therefore necessarily
    trusted-local" is NOT a shared-query authorization rule, because the test
    habit of omitting a source is not production trust evidence, and an item
    whose provenance cannot be verified must not be waved through.

    * a source stamped :data:`TRUST_LOCAL` -> trusted-local (the ONLY local case);
    * a ``managed`` stamp, an unstamped/unknown/``None`` stamp (a legacy source
      the migration did not classify local, or a dangling source_id whose row is
      gone) -> managed;
    * a SOURCELESS item (``has_source`` False) -> MANAGED. Absence of a source is
      not proof of no external ACL. Production ingestion always attaches a
      source, so a genuinely-local item reaches the store through a stamped local
      creator (folder/file/vault/artifact/agent); a sourceless row is
      unverifiable provenance and is denied to a shared query. The single-user
      local library keeps its own admitted content because that content is
      sourced+stamped; only records with NO provenance evidence at all are
      locked out (which, if any exist in a real library, are reported for
      explicit migration -- never auto-backfilled to local).

    The decision reads the stored provenance stamp only; it never re-guesses
    trust from a source_type string, so a new/misspelled cloud connector type,
    or a managed source missing its per-item flag, cannot be waved through as
    local. The stamp itself does not prove the issuer is trusted -- only a
    trusted local creator may issue :data:`TRUST_LOCAL` (see
    store.initial_trust_class / add_source, which refuse a caller-forged local
    stamp for a non-local creator type).
    """
    if not has_source:
        return True
    return trust_class != TRUST_LOCAL


@dataclass(frozen=True)
class AccessContext:
    """The verified identity a knowledge query runs as.

    ``subject`` is the stable, authenticated principal id and ``tenant`` is the
    org/workspace boundary it belongs to. For a MANAGED cloud/structured item,
    these must be the PROVIDER-mapped identity (the vendor subject/tenant the
    authenticated caller resolves to -- see W01's Binding.subject_ref /
    tenant_ref), NOT a raw KiroCrew session key and NOT a value the model chose
    or an inbound payload asserted.

    ``groups`` are additional grant-bearing identifiers the subject holds within
    the same tenant (team ids, org roles). They participate in the subject test
    exactly like ``subject`` does.

    ``bypass_acl`` marks a caller with NO cross-identity boundary -- the local
    single-user library. It is NOT "admit everything": it lets TRUSTED-LOCAL
    items be seen without a grant, but a MANAGED item is still gated and, because
    a bypass context carries no verifiable provider-mapped subject, a managed
    item is DENIED under it. Use :data:`LOCAL_LIBRARY` for this. A
    shared/multi-tenant caller MUST set a real subject/tenant instead.
    """

    subject: str
    tenant: str
    groups: frozenset[str] = field(default_factory=frozenset)
    bypass_acl: bool = False

    def __post_init__(self) -> None:
        if not self.bypass_acl and not self.subject:
            raise ValueError(
                "AccessContext requires a non-empty subject unless bypass_acl=True; "
                "resolve the authenticated principal at the caller boundary, or use "
                "acl.LOCAL_LIBRARY for a single-user local library."
            )

    @property
    def subject_ids(self) -> frozenset[str]:
        """Every identifier that satisfies a grant's subject test for this context."""
        return frozenset({self.subject, *self.groups})


# The local single-user library context: no cross-identity boundary. Admits
# trusted-local items without a grant; DENIES managed items (it has no verifiable
# provider-mapped subject to check them against). Spelled with sentinel
# subject/tenant so an audit log records unambiguously which path ran.
LOCAL_LIBRARY = AccessContext(
    subject="<local-single-user>", tenant="<local>", bypass_acl=True
)

# Back-compat alias: the earlier name for the local context. It NO LONGER means
# "allow everything" -- managed items are gated even under it. Kept only so an
# external caller importing the old name still resolves; new code uses
# LOCAL_LIBRARY.
ALLOW_ALL = LOCAL_LIBRARY

#: Default staleness window for a managed item's revalidation, in seconds. A
#: grant last confirmed current more than this long ago is treated as stale and
#: must be re-confirmed by the revalidation hook before the item is served.
DEFAULT_STALENESS_SECS = 300.0


@dataclass(frozen=True)
class ItemGrant:
    """The decoded ACL grant record for one knowledge item.

    ``subjects`` is the set of subject ids (or :data:`PUBLIC_SUBJECT`) allowed to
    see the item; ``tenant`` is the tenant the grant belongs to. ``acl_version``
    is a monotonic marker the store bumps on every rewrite, so a cached decision
    keyed on ``(item_id, acl_version)`` is invalidated the moment a revoke
    rewrites the record.

    ``managed`` marks a cloud/structured item (see :func:`is_managed_source_type`)
    that must go through the current-subject check AND revalidation. ``fresh_as_of``
    is the epoch-seconds timestamp at which this grant was last confirmed current
    against the provider (0.0 = never / ingest-time only), which the freshness
    check reads.

    A grant that could not be parsed is :data:`UNREADABLE_GRANT` (deny).
    """

    subjects: frozenset[str]
    tenant: str
    acl_version: int = 0
    readable: bool = True
    managed: bool = False
    fresh_as_of: float = 0.0

    @classmethod
    def from_row(
        cls,
        subjects_json: str | bytes | None,
        tenant: str | None,
        acl_version: int | None,
        *,
        managed: bool = False,
        fresh_as_of: float | None = 0.0,
    ) -> "ItemGrant":
        """Decode a stored grant row. Any malformed field yields an unreadable grant."""
        if tenant is None:
            return UNREADABLE_GRANT
        try:
            decoded = json.loads(subjects_json) if subjects_json else []
        except (json.JSONDecodeError, TypeError):
            return UNREADABLE_GRANT
        if not isinstance(decoded, list) or not all(isinstance(s, str) for s in decoded):
            return UNREADABLE_GRANT
        return cls(
            subjects=frozenset(decoded),
            tenant=tenant,
            acl_version=int(acl_version) if acl_version is not None else 0,
            managed=managed,
            fresh_as_of=float(fresh_as_of) if fresh_as_of else 0.0,
        )


# The grant an item has when its permissions could not be read (missing row,
# malformed JSON, wrong type). Distinct from an item that is *known* to be
# private-to-nobody: this is "we do not know", and the policy denies it.
UNREADABLE_GRANT = ItemGrant(subjects=frozenset(), tenant="", acl_version=0, readable=False)

# The grant meaning "no record exists for this item at all". Also deny for a
# managed item; a trusted-local item with no grant is handled by the classifier,
# not by this sentinel.
MISSING_GRANT = ItemGrant(subjects=frozenset(), tenant="", acl_version=0, readable=False)


class RevalidationOutcome:
    """The three answers a revalidation hook can give for one managed grant."""

    #: The provider confirms the grant is current: serve the item.
    FRESH = "fresh"
    #: The provider says access is revoked/denied now: drop the item.
    REVOKED = "revoked"
    #: The provider could not be reached / no hook is wired: fail-closed (drop).
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class ProviderResourceRef:
    """WHICH provider object a managed item was ingested from -- the resource
    locator a revalidation probe needs to ask 'does this subject still have
    access to THIS?'.

    This is the per-source provenance the ingest path must persist alongside the
    grant (it is NOT derivable from item text). The exact fields a given provider
    needs vary, so ``locator`` is a provider-shaped dict, but every managed
    ingest MUST populate at least ``provider`` + enough of ``locator`` to
    re-identify the object:

    * ``provider``   -- the connector id (e.g. 'sharepoint', 'google_drive',
                        'salesforce', 'github_structured'); selects which probe
                        implementation and which credential binding to use.
    * ``account``    -- the provider account / tenant the object lives in (a
                        Graph tenant id, a Salesforce org id, a Drive
                        corpora/driveId). Distinct from AccessContext.tenant,
                        which is the KiroCrew-side boundary; this is the VENDOR
                        side.
    * ``resource_id``-- the object's stable provider id (a driveItem id, a
                        SharePoint listItem id, an SObject id, a repo+path).
    * ``locator``    -- any additional provider-shaped coordinates the probe
                        needs (site id, drive id, list id, object type, field
                        set). Free-form per provider, kept opaque here.

    Examples of the minimal locator per provider family (for the connector
    owners wiring the probe):
      sharepoint/onedrive: {siteId|driveId, itemId, [listId]}
      google_drive:        {fileId, [driveId/corpora]}
      salesforce:          {sobjectType, recordId, [fieldSet]}
      github_structured:   {owner, repo, path|objectId}
    """

    provider: str
    account: str = ""
    resource_id: str = ""
    locator: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RevalidationRequest:
    """The complete input a :class:`RevalidationHook` needs to probe one item.

    Assembled by the retriever/caller boundary from three sources, so the hook
    implementation never has to reach back into the store or guess identity:

    * ``ctx``          -- the VERIFIED querying identity. For a managed item this
                          MUST be the PROVIDER-MAPPED subject/tenant (W01
                          Binding.subject_ref / tenant_ref from the verifier),
                          NOT a raw KiroCrew session key and NOT one binding
                          reused for every source.
    * ``item_id``      -- the KiroCrew knowledge item under test (audit key).
    * ``grant``        -- the stored grant (subjects/tenant/acl_version/managed/
                          fresh_as_of) -- the snapshot being revalidated.
    * ``resource``     -- WHICH provider object to probe (:class:`ProviderResourceRef`).
    * ``credential_ref``-- an OPAQUE handle naming the credential binding to call
                          the provider AS this subject. It is a reference the
                          host resolves at probe time (a vault key / connection
                          id), never a raw secret carried here. Which binding is
                          correct is decided by W01's trusted binding association
                          for (provider, account, subject) -- this field only
                          NAMES it.

    The hook returns a :class:`RevalidationOutcome`. Any field it cannot resolve
    (missing resource, unresolvable credential binding, provider error) MUST
    yield UNVERIFIABLE, never FRESH.
    """

    ctx: "AccessContext"
    item_id: str
    grant: "ItemGrant"
    resource: ProviderResourceRef | None = None
    credential_ref: str = ""


@runtime_checkable
class RevalidationHook(Protocol):
    """The provider live-permission interface (a DEPENDENCY, not implemented here).

    Given the querying context and a managed item's grant, answers whether the
    provider STILL grants that subject access RIGHT NOW. This is what turns a
    static ingest-time snapshot into a query-time real check: without it, a
    managed grant is :data:`RevalidationOutcome.UNVERIFIABLE` and therefore
    denied.

    The retriever calls ``revalidate(ctx, item_id, grant)`` -- the minimal keys
    it holds at query time. A REAL implementation additionally needs, per item,
    the :class:`ProviderResourceRef` (which provider object to probe) and the
    ``credential_ref`` (which binding to call the provider AS this subject) --
    together the :class:`RevalidationRequest`. Those two are the named INGEST-SIDE
    + W01 dependencies: the ingest path must persist the resource ref alongside
    the grant, and W01's trusted binding association resolves the credential for
    (provider, account, subject). An implementation looks those up by ``item_id``
    (and ``ctx``) to build the full request; until they exist it cannot answer
    FRESH and every managed grant stays denied.

    An implementation performs the provider I/O (a permission probe, a delta/ACL
    read) and MAY cache within the staleness window; it must return
    :data:`RevalidationOutcome.UNVERIFIABLE` on any error/timeout/unresolvable
    input rather than guessing FRESH, so the fail-closed posture holds end to
    end.
    """

    def revalidate(self, ctx: "AccessContext", item_id: str, grant: "ItemGrant") -> str:
        ...


@runtime_checkable
class AclPolicy(Protocol):
    """Decides whether an :class:`AccessContext` may see one item's grant."""

    def allows(
        self,
        ctx: AccessContext,
        grant: ItemGrant,
        *,
        revalidation: str = RevalidationOutcome.UNVERIFIABLE,
        now: float | None = None,
        staleness_secs: float = DEFAULT_STALENESS_SECS,
    ) -> bool:
        ...


class SubjectTenantAclPolicy:
    """Fail-closed subject+tenant visibility with managed-item revalidation.

    A TRUSTED-LOCAL item (``grant.managed`` false) is visible to a bypass context
    without a grant, and otherwise by the subject/tenant test below.

    A MANAGED item (``grant.managed`` true) is visible iff ALL of:

    * the grant is readable, AND
    * the context is NOT a bypass context -- a local single-user context carries
      no verifiable provider-mapped subject, so it can never see a managed item,
      AND
    * the subject/tenant test passes (public sentinel or subject intersection;
      same email in a different tenant does NOT match -- ACL-06), AND
    * the grant is FRESH: either the revalidation hook returned
      :data:`RevalidationOutcome.FRESH`, OR the stored ``fresh_as_of`` is within
      ``staleness_secs`` of ``now``. A ``REVOKED`` or ``UNVERIFIABLE`` outcome
      denies even a subject-matching grant, and a stale ``fresh_as_of`` with no
      fresh hook answer denies too. This is the second root-cause fix: a static
      grant that was never revalidated cannot be served as if it were live.
    """

    def allows(
        self,
        ctx: AccessContext,
        grant: ItemGrant,
        *,
        revalidation: str = RevalidationOutcome.UNVERIFIABLE,
        now: float | None = None,
        staleness_secs: float = DEFAULT_STALENESS_SECS,
    ) -> bool:
        if not grant.readable:
            return False

        if not grant.managed:
            # Trusted-local material: a bypass (local) context sees it, and an
            # enforcing context still gets the subject/tenant test (a local item
            # MAY carry a grant, e.g. a shared vault, in which case it is
            # honoured).
            if ctx.bypass_acl:
                return True
            return self._subject_tenant_ok(ctx, grant)

        # Managed item from here on.
        if ctx.bypass_acl:
            # No verifiable provider-mapped subject to check against: deny THIS
            # item (not the whole library).
            return False
        if not self._subject_tenant_ok(ctx, grant):
            return False
        return self._is_fresh(grant, revalidation, now, staleness_secs)

    @staticmethod
    def _subject_tenant_ok(ctx: AccessContext, grant: ItemGrant) -> bool:
        if grant.tenant != PUBLIC_TENANT and grant.tenant != ctx.tenant:
            return False
        if PUBLIC_SUBJECT in grant.subjects:
            return True
        return bool(grant.subjects & ctx.subject_ids)

    @staticmethod
    def _is_fresh(
        grant: ItemGrant, revalidation: str, now: float | None, staleness_secs: float
    ) -> bool:
        if revalidation == RevalidationOutcome.FRESH:
            return True
        if revalidation == RevalidationOutcome.REVOKED:
            return False
        # UNVERIFIABLE: fall back to the stored freshness stamp. A grant whose
        # last confirmation is within the window is served; anything older (or a
        # never-confirmed grant, fresh_as_of == 0.0) is stale -> deny. This is
        # what makes "no revalidation hook wired" fail-closed for a managed item
        # rather than serving an ingest-time snapshot forever.
        if grant.fresh_as_of <= 0.0:
            return False
        current = time.time() if now is None else now
        return (current - grant.fresh_as_of) <= staleness_secs


#: The policy used unless a caller injects its own. Stateless, so one shared
#: instance serves every retriever.
DEFAULT_POLICY: AclPolicy = SubjectTenantAclPolicy()
