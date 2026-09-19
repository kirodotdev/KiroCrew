"""Real query-time Google Drive permission probe -- through W01, no HTTP here.

The knowledge retriever turns a MANAGED item's static grant into a LIVE decision
by asking a :class:`~kiro_crew.knowledge.acl.RevalidationHook` whether the
querying subject STILL has access RIGHT NOW. This module is the Google Drive fill
of that hook. It answers, per candidate per query, whether subject S still has
access to Drive file F, as one of :data:`~kiro_crew.knowledge.acl.RevalidationOutcome`'s
three values -- ``FRESH`` / ``REVOKED`` / ``UNVERIFIABLE`` (deny, fail-closed).

Two properties the brief requires, enforced by construction:

* **The querying identity is verified LIVE, never self-asserted, and the call
  goes through W01.** The probe issues a Drive ``files.get`` for F through an
  injected :class:`SubjectOperationRunner` -- W01's ``execute`` bound to the
  credential custody of the VERIFIED subject S (its handle / binding). There is no
  token, session, HTTP or vault in this module. A subject that resolves to no
  binding cannot be run AS, so the runner yields no authorization and the probe
  returns UNVERIFIABLE -- there is no ``email``-string impersonation path, because
  the subject can only ever be probed with its OWN W01 binding.

* **A revocation takes effect on the NEXT query.** No grant is cached across
  queries: every :meth:`revalidate` runs a fresh ``files.get`` as the subject. The
  moment Drive stops returning F to S (permission removed at source), the next
  query's probe sees a 403/404 and returns REVOKED, and the store's grant is
  rewritten to deny (bumping ``acl_version``) so it holds for every later query.
  There is no TTL that could serve a revoked file for a window.

A short, per-probe cache keyed on ``(item, subject, acl_version)`` is a same-query
de-dup ONLY, invalidated the instant ``acl_version`` bumps (which every
revoke/refresh does), so it can never outlive a revocation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from kiro_crew.knowledge.acl import (
    AccessContext,
    ItemGrant,
    ProviderResourceRef,
    RevalidationOutcome,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "google_drive"


class SubjectOperationRunner(Protocol):
    """Runs ONE Drive operation AS a verified subject, through W01's executor.

    THIS is the anti-impersonation seam. Given the verified querying subject, the
    Drive account the candidate lives in, and the operation descriptor + args, the
    host resolves the W01 binding/handle that association holds for
    ``(google_drive, account, subject)`` and runs the operation through
    :func:`~kiro_crew.connections.control_plane.executor.execute`. It returns a
    :class:`ProbeOutcome` -- ``granted`` when Drive served the file to that
    subject, ``revoked`` when Drive denied/hid it (403/404), ``unverifiable`` when
    the subject holds no binding or the call could not be completed.

    The host owns credential custody; this module never sees a token. A subject
    with no binding yields ``unverifiable`` -- the probe NEVER borrows another
    identity's binding, so a self-reported email cannot be believed.
    """

    def probe_file(self, subject: str, account: str, file_id: str) -> "ProbeOutcome": ...


@dataclass(frozen=True)
class ProbeOutcome:
    """One of exactly three answers the subject-scoped runner gives per file probe.

    ``kind`` is ``granted`` / ``revoked`` / ``unverifiable``. Use the module
    sentinels :data:`GRANTED` / :data:`REVOKED` / :data:`UNVERIFIABLE`.
    """

    kind: str


# Module sentinels so a runner returns one of exactly three values.
GRANTED = ProbeOutcome(kind="granted")
REVOKED = ProbeOutcome(kind="revoked")
UNVERIFIABLE = ProbeOutcome(kind="unverifiable")


@dataclass
class _CacheEntry:
    outcome: str
    observed_at: float


class GoogleDriveRevalidationHook:
    """Query-time Drive permission probe implementing the RevalidationHook shape.

    Wired on the dashboard app as ``app["knowledge_revalidator"]`` for a
    deployment that serves shared Drive content. :meth:`revalidate` matches the
    hook signature the retriever calls: ``revalidate(ctx, item_id, grant)``. It
    resolves the item's :class:`ProviderResourceRef` from the store by
    ``item_id`` (the ingest path persisted it) to know WHICH Drive file to probe;
    only a ``google_drive`` ref is this hook's to answer.
    """

    def __init__(
        self,
        store,
        runner: SubjectOperationRunner,
        *,
        cache_ttl_secs: float = 5.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._runner = runner
        self._cache_ttl = cache_ttl_secs
        self._now = now
        self._cache: dict[tuple[str, str, int], _CacheEntry] = {}

    def revalidate(self, ctx: AccessContext, item_id: str, grant: ItemGrant) -> str:
        """FRESH / REVOKED / UNVERIFIABLE for one managed Drive item.

        Fail-closed at every uncertain step: no ref, a ref for another provider,
        a bypass/subject-less context, a runner that could not authorize -> all
        UNVERIFIABLE. Only a concrete grant/deny from Drive (through W01) decides.
        """
        resource = self._resource_for(item_id)
        if resource is None or resource.provider != PROVIDER_ID:
            return RevalidationOutcome.UNVERIFIABLE

        file_id = _file_id_of(resource)
        if not file_id:
            return RevalidationOutcome.UNVERIFIABLE

        if ctx.bypass_acl or not ctx.subject:
            return RevalidationOutcome.UNVERIFIABLE

        cache_key = (item_id, ctx.subject, grant.acl_version)
        cached = self._cache.get(cache_key)
        now = self._now()
        if cached is not None and (now - cached.observed_at) <= self._cache_ttl:
            return cached.outcome

        outcome = self._probe(ctx.subject, resource.account, file_id)
        self._cache[cache_key] = _CacheEntry(outcome=outcome, observed_at=now)
        self._record_outcome(item_id, ctx, grant, outcome)
        return outcome

    def _probe(self, subject: str, account: str, file_id: str) -> str:
        """One live files.get AS the subject, through W01. Maps to an outcome."""
        try:
            result = self._runner.probe_file(subject, account, file_id)
        except Exception:
            logger.warning(
                "Drive ACL probe runner raised for file %s; unverifiable " "(fail-closed)",
                file_id,
                exc_info=True,
            )
            return RevalidationOutcome.UNVERIFIABLE
        if result is GRANTED or getattr(result, "kind", None) == "granted":
            return RevalidationOutcome.FRESH
        if result is REVOKED or getattr(result, "kind", None) == "revoked":
            return RevalidationOutcome.REVOKED
        return RevalidationOutcome.UNVERIFIABLE

    def _resource_for(self, item_id: str) -> Optional[ProviderResourceRef]:
        try:
            grants = self._store.get_item_grants([item_id])
        except Exception:
            logger.warning(
                "Drive ACL probe could not read grant for item %s; unverifiable",
                item_id,
                exc_info=True,
            )
            return None
        raw = grants.get(item_id)
        if not raw:
            return None
        return ProviderResourceRef.from_json(raw.get("resource_ref"))

    def _record_outcome(
        self, item_id: str, ctx: AccessContext, grant: ItemGrant, outcome: str
    ) -> None:
        """Persist a confirmed observation so it holds for the NEXT query too.

        REVOKED -> rewrite the grant to deny (empty subjects), bumping acl_version
        so it is effective immediately on the next query. FRESH -> stamp
        fresh_as_of now (keep the confirmed subject visible). UNVERIFIABLE -> no
        write (nothing authoritative learned).
        """
        try:
            if outcome == RevalidationOutcome.REVOKED:
                self._store.revoke_item_acl(item_id)
            elif outcome == RevalidationOutcome.FRESH:
                self._store.mark_item_acl_revalidated(
                    item_id,
                    sorted(set(grant.subjects) | {ctx.subject}),
                    fresh_as_of=self._now(),
                    tenant=ctx.tenant,
                )
        except Exception:
            logger.warning(
                "Drive ACL probe could not persist %s for item %s", outcome, item_id, exc_info=True
            )


def _file_id_of(resource: ProviderResourceRef) -> str:
    loc = resource.locator or {}
    fid = loc.get("fileId")
    if isinstance(fid, str) and fid:
        return fid
    return resource.resource_id or ""


__all__ = [
    "GRANTED",
    "GoogleDriveRevalidationHook",
    "PROVIDER_ID",
    "ProbeOutcome",
    "REVOKED",
    "SubjectOperationRunner",
    "UNVERIFIABLE",
]
