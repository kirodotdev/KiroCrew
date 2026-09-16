"""Query-time access control for the shared Knowledge Library.

The retriever's ``source_id``/``namespace`` filters are relevance labels, NOT a
security boundary (their own docstrings say so, and the graph leg ignores them
entirely). A knowledge base that ingests *shared* sources under an admin/service
identity must therefore gate what any given querying user actually sees at
RETRIEVAL time against that user's OWN permission -- never the ingestion
identity's, never a filter the caller could widen, and never a permission
snapshot taken at ingest time (see KB-01/ACL-05/ACL-09 in the connector
production stack's shared contracts).

This module carries the two primitives that make that possible without a second
knowledge base:

* :class:`AccessContext` -- the *verified* subject and tenant a query runs as.
  It is constructed from the authenticated call context by the caller boundary
  (the MCP tool / dashboard handler), never from a field the model or an inbound
  message self-reports. A missing/unauthenticated context is
  :data:`DENY_ALL`, not "everything".

* :class:`AclPolicy` -- decides, for one item's stored grant record, whether a
  given :class:`AccessContext` may see it. The default
  :class:`SubjectTenantAclPolicy` is deliberately FAIL-CLOSED: an item with no
  grant record, an item whose grant record cannot be read, a tenant mismatch, or
  a subject not on the grant list all resolve to *deny*. The one and only way to
  see everything is the explicit :data:`ALLOW_ALL` context, which the local
  single-user paths pass on purpose and which a shared/multi-tenant caller must
  never mint.

Nothing here talks to a vendor or performs I/O: it decides visibility over grant
records the store already holds. The store owns *how* those records are written
(at ingest time) and read (at query time); this module owns the DECISION.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

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


@dataclass(frozen=True)
class AccessContext:
    """The verified identity a knowledge query runs as.

    ``subject`` is the stable, authenticated principal id (a dashboard user id,
    an app identity, a service principal) and ``tenant`` is the org/workspace
    boundary it belongs to. Both come from the authenticated call context at the
    caller boundary -- resolved from the transport's own verified identity, never
    from a parameter the model chose or an inbound payload asserted.

    ``groups`` are additional grant-bearing identifiers the subject holds within
    the same tenant (team ids, org roles). They participate in the subject test
    exactly like ``subject`` does, so a grant naming a group the subject is a
    member of is honoured.

    ``bypass_acl`` is the single-user / local-library escape hatch: when true the
    policy admits every item. It exists ONLY for the paths that have no
    meaningful multi-identity boundary (a personal local Knowledge Library), and
    a shared/multi-tenant caller MUST NOT set it -- see :data:`ALLOW_ALL`. It is
    a field rather than a separate policy so the retriever threads exactly one
    object through every leg.
    """

    subject: str
    tenant: str
    groups: frozenset[str] = field(default_factory=frozenset)
    bypass_acl: bool = False

    def __post_init__(self) -> None:
        # A bypass context is the only one allowed to carry an empty subject:
        # every enforcing context must name a real principal, because an empty
        # subject that reached the subject test would match nothing (correct) but
        # signals a caller that forgot to resolve identity -- fail loudly at
        # construction rather than silently denying everything and looking like
        # an ACL bug.
        if not self.bypass_acl and not self.subject:
            raise ValueError(
                "AccessContext requires a non-empty subject unless bypass_acl=True; "
                "resolve the authenticated principal at the caller boundary, or use "
                "acl.ALLOW_ALL for a single-user local library."
            )

    @property
    def subject_ids(self) -> frozenset[str]:
        """Every identifier that satisfies a grant's subject test for this context."""
        return frozenset({self.subject, *self.groups})


# The single-user / local-library context: admit everything. Named and passed on
# purpose by paths with no multi-identity boundary; never to be minted by a
# shared/multi-tenant caller. Spelled with sentinel subject/tenant so an audit
# log that records the context is unambiguous about which path ran.
ALLOW_ALL = AccessContext(subject="<local-single-user>", tenant="<local>", bypass_acl=True)


@dataclass(frozen=True)
class ItemGrant:
    """The decoded ACL grant record for one knowledge item.

    ``subjects`` is the set of subject ids (or :data:`PUBLIC_SUBJECT`) allowed to
    see the item; ``tenant`` is the tenant the grant belongs to. ``acl_version``
    is an opaque monotonic marker the store bumps whenever the grant changes, so
    a cached decision keyed on ``(item_id, acl_version)`` is invalidated the
    moment a revoke rewrites the record.

    A grant that could not be parsed is represented by :data:`UNREADABLE_GRANT`,
    which the policy treats as deny (fail-closed) -- an item whose permissions we
    cannot read is an item we must not serve.
    """

    subjects: frozenset[str]
    tenant: str
    acl_version: int = 0
    readable: bool = True

    @classmethod
    def from_row(cls, subjects_json: str | bytes | None, tenant: str | None,
                 acl_version: int | None) -> "ItemGrant":
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
        )


# The grant an item has when its permissions could not be read (missing row,
# malformed JSON, wrong type). Distinct from an item that is *known* to be
# private-to-nobody: this is "we do not know", and the policy denies it.
UNREADABLE_GRANT = ItemGrant(subjects=frozenset(), tenant="", acl_version=0, readable=False)

# The grant meaning "no record exists for this item at all". Also deny: absence
# of a grant is not permission. Kept distinct from UNREADABLE_GRANT only so an
# audit/debug caller can tell "never written" from "written but corrupt".
MISSING_GRANT = ItemGrant(subjects=frozenset(), tenant="", acl_version=0, readable=False)


@runtime_checkable
class AclPolicy(Protocol):
    """Decides whether an :class:`AccessContext` may see one item's grant."""

    def allows(self, ctx: AccessContext, grant: ItemGrant) -> bool:
        ...


class SubjectTenantAclPolicy:
    """Fail-closed subject+tenant visibility.

    An item is visible to ``ctx`` iff ALL of:

    * the grant is readable (a missing/corrupt grant is denied), AND
    * the tenants match -- the grant's tenant equals the context's tenant, OR the
      grant is marked :data:`PUBLIC_TENANT` (cross-tenant public); the same email
      in a different tenant is a different identity and does not match
      (ACL-06), AND
    * the subject test passes -- the grant lists :data:`PUBLIC_SUBJECT`, OR the
      grant's subject set intersects the context's ``subject_ids``.

    :attr:`AccessContext.bypass_acl` short-circuits to allow, for the local
    single-user library only.
    """

    def allows(self, ctx: AccessContext, grant: ItemGrant) -> bool:
        if ctx.bypass_acl:
            return True
        if not grant.readable:
            return False
        if grant.tenant != PUBLIC_TENANT and grant.tenant != ctx.tenant:
            return False
        if PUBLIC_SUBJECT in grant.subjects:
            return True
        return bool(grant.subjects & ctx.subject_ids)


#: The policy used unless a caller injects its own. Stateless, so one shared
#: instance serves every retriever.
DEFAULT_POLICY: AclPolicy = SubjectTenantAclPolicy()
