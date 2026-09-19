"""W01 · L04: the binding lifecycle -- a TRUSTED, PERSISTENT store, cross-process
single-writer refresh rotation, and revoke fencing by ``generation``.

L02 (:mod:`kiro_crew.connections.control_plane.binding`) landed the ``Binding``
record and a ``resolve_binding_for_principal`` that keys on a VERIFIED identity.
But its resolver picks the match out of an ``Iterable`` the CALLER hands in --
so the candidate set is caller-controlled, and a caller can slip in a binding it
should not own and have the resolve "legitimately" hit it. That is the fifth
recurrence of the trusted-source discipline (L07 recorded provenance -> L08
returned views -> the executor's inputs -> secret resolution -> now the
candidate set): the same class of defect, not a new point to patch. This module
closes it as a pattern:

1. **The candidate set comes from a TRUSTED PERSISTENT STORE, never the caller.**
   :class:`BindingStore` reads bindings from a file under :func:`config_dir`,
   and :meth:`BindingStore.resolve` matches ONLY over what that store holds. A
   caller cannot present a candidate the store never admitted, and the store
   survives a process restart, so resolution is reproducible across restarts.
   **No new vault.** A secret is STILL only a reference (L02's ``SecretRef`` /
   the existing ``SecretVault`` / ``client_secret_name`` convention); what this
   module persists is the BINDING RECORD, not the secret value.

2. **``(deployment, account)`` is the uniqueness domain, and it is ENFORCED.** A
   binding is uniquely determined by
   ``(deployment_id, service_id, subject_ref, tenant_ref)`` -- the PROVIDER-SIDE
   deployment that hosts the account (a specific GitHub Enterprise host, a
   Salesforce org, a Microsoft Graph tenant deployment), plus the provider
   account it names (service + verified subject + verified tenant). Note the
   direction: ``deployment_id`` is a PROVIDER fact, NOT a Kiro Crew instance id;
   using our own instance id here would merge two different provider deployments
   into one binding and split one provider deployment seen from two Kiro
   instances into two -- exactly backwards. :meth:`BindingStore.insert` REFUSES
   (:class:`BindingUniquenessError`) a second binding that collides on that
   domain, AND refuses a re-insert under an existing ``binding_id`` whose domain
   fields changed (an illegal mutation, never a silent overwrite). The domain is
   decidable from stored fields alone, because the downstream ACL's
   ``resolve(principal, provider, account)`` keys on it.

3. **Refresh rotation is a CROSS-PROCESS SINGLE WRITER around the REAL refresh.**
   Two processes may both try to rotate one binding's secret at once (a
   scheduled refresh racing an on-demand one). :meth:`BindingStore.rotate`
   serializes them through :func:`platform_compat.file_lock` /
   :func:`platform_compat.acquire_lock` (NEVER a raw ``fcntl`` -- a hard
   cross-platform constraint), and the caller's REAL token-refresh callable is
   invoked INSIDE that lock -- so the provider's token endpoint is hit exactly
   ONCE under contention, not once per process. The LOSER does NOT refresh and
   does NOT rotate: on acquiring the lock it re-reads state, sees the generation
   already advanced past what it observed, and returns the winner's result
   without calling the refresh callable at all.

4. **Revoke FENCES older AND forged generations, on identity.**
   :meth:`BindingStore.revoke` raises the binding's live ``generation`` (L02 left
   the counter and its increment semantics here for exactly this). After a
   revoke, :meth:`BindingStore.assert_live` and :meth:`BindingStore.resolve`
   refuse a handle unless its ``generation`` EXACTLY equals the store's current
   live generation for THAT SAME binding identity -- so a stale pre-revoke
   handle is refused, a forged FUTURE generation (e.g. ``999``) is refused, and a
   handle carrying another binding's (numerically valid) generation is refused
   because the identity does not match. Equality-on-identity, not "not greater"
   and not "less than".

5. **The Kiro principal -> authorization link is REAL, not a doc claim.** Each
   stored binding records the ``kiro_principal`` that is authorized to use it,
   and :meth:`BindingStore.resolve` requires the CALLER's principal to equal it
   -- a different principal, even with a correct provider identity, does not
   resolve. This is the ``resolve(principal, provider, account)`` seam the
   downstream ACL calls, enforced in code.

The old-custody iron law, restated
-----------------------------------
This module **never reads, copies, or references an old kiro-cli OAuth token.**
It persists a binding RECORD (ids, verified refs, a secret NAME, a generation),
never a secret value and never a filesystem path into kiro-cli's token store. A
rotation advances a reference and a generation; it does not move a legacy token.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, NotRequired, Protocol, TypedDict

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.connections.control_plane.binding import (
    Binding,
    BindingResolutionError,
    SecretRef,
    SubjectTenantVerifier,
    next_generation,
)
from kiro_crew.connections.control_plane.operation import CredentialMode, ServiceId

#: Bumped when the persisted store's on-disk shape changes, mirroring the
#: sibling control-plane modules' schema-version constants.
LIFECYCLE_SCHEMA_VERSION = 1

#: The persisted binding store lives under ``config_dir()/control-plane-bindings/``
#: -- a TOP-LEVEL directory in the data home, deliberately NOT nested under
#: ``connections/``. The store is the SINGLE trusted source resolution reads from
#: (a caller never supplies the candidate set), and it is registered as an
#: OS-sandbox read-only leaf plus a file-tool write-protected path. That seal only
#: holds for a top-level leaf: a nested leaf can be bypassed by renaming its
#: writable parent (the Linux bind mount follows the moved parent, leaving the
#: original path free for an agent-planted replacement), and the ``connections/``
#: directory is agent-writable and shared with the tool-alias manifest and the
#: status state file, so fencing it whole would be far too broad. Its own top-level
#: directory has no agent-writable ancestor inside the data home.
_STORE_SUBDIR = "control-plane-bindings"
_STORE_FILENAME = "bindings.json"

#: The cross-process lock file, BESIDE the store rather than on it: ``atomic_write``
#: publishes by renaming a fresh inode over the store path, which would swap the
#: file out from under an advisory lock held on the store's own fd. A dedicated
#: lock file is stable across the rename, so every process serializes on one
#: inode. Mirrors ``acp/seed_provenance.py``'s ``.lock`` sidecar discipline. It
#: lives in the same top-level ``control-plane-bindings/`` directory, so the single
#: read-only directory leaf covers the store, the lock, and every temp file the
#: atomic writer creates there.
_LOCK_FILENAME = "bindings.lock"


def _store_dir() -> Path:
    return config_dir() / _STORE_SUBDIR


def store_path() -> Path:
    """Absolute path of the persisted binding store (see module docstring).

    Under :func:`config_dir`, so it moves with a ``KIROCREW_HOME`` override and
    is the same file every Crew process on the host reads and writes. Resolution
    reads from HERE, never from a caller-supplied set.
    """

    return _store_dir() / _STORE_FILENAME


class StoredBinding(TypedDict):
    """A persisted binding: L02's :class:`Binding` plus the lifecycle fields.

    L02's ``Binding`` is embedded VERBATIM under ``binding`` -- this module does
    not change that type or its meaning; it wraps it. The added fields are the
    lifecycle state L04 owns:

    ``deployment_id`` -- the PROVIDER-SIDE deployment that hosts the account (a
    GitHub Enterprise host, a Salesforce org, a Microsoft Graph tenant
    deployment), the first component of the uniqueness domain. It is a provider
    fact, NOT a Kiro Crew instance id: two accounts on two provider deployments
    are two bindings, and one provider deployment reached from two Kiro instances
    is still ONE binding. ``kiro_principal`` -- the authenticated Kiro Crew
    principal authorized to use this binding; :meth:`BindingStore.resolve`
    requires the caller's principal to equal it. ``live_generation`` -- the
    CURRENT authoritative generation for this binding; a revoke raises it, and a
    credential whose generation is not EXACTLY this (for this identity) is
    fenced. It starts equal to the embedded binding's own ``generation`` and only
    ever increases. ``revoked`` -- whether the binding has been revoked (a revoked
    binding resolves to nothing and fences every generation). ``updated_at`` --
    absolute POSIX-seconds UTC of the last lifecycle write.
    """

    binding: Binding
    deployment_id: str
    kiro_principal: str
    live_generation: int
    revoked: bool
    updated_at: float
    # The VENDOR-side account this binding is for (a GitHub org/login, a Graph
    # tenant id / driveId, a Salesforce org id, a Slack workspace id) -- the ACL's
    # ``account`` axis. Recorded so a trusted, store-backed account -> deployment
    # mapping is possible (see StoreBackedAccountToDeployment in the ACL adapter).
    # NotRequired so an older record without it is still well-formed; a record
    # missing it simply does not participate in account->deployment resolution.
    account: NotRequired[str]
    # The provider-side ENDPOINT/host this deployment lives at (a Salesforce
    # instanceUrl, a Graph endpoint host, a GHE host). It is the discriminator
    # that tells apart the SAME account name hosted on two different provider
    # deployments -- the store is the sole authority on which endpoints exist, so
    # a caller-supplied endpoint not registered here is refused. NotRequired for
    # the same back-compat reason as ``account``.
    endpoint: NotRequired[str]


class BindingStoreCorruptError(Exception):
    """Raised when the on-disk store exists but cannot be trusted.

    A malformed store file (not JSON, wrong top-level shape) is NOT read as an
    empty store: doing so would let a WRITE republish a fresh file that silently
    ERASES every binding the corrupt bytes might still represent, and would let an
    insert that should collide "become legal" again. So a write path FAILS CLOSED
    on a corrupt store -- it refuses rather than overwriting -- and honestly
    reports "unreadable", never "empty". A read path surfaces the same signal so a
    resolve does not silently answer "no binding" for a store that is merely
    unreadable.
    """


class BindingUniquenessError(Exception):
    """Raised when an insert would collide on the ``(deployment, account)`` domain.

    The store REFUSES a second binding whose
    ``(deployment_id, service_id, subject_ref, tenant_ref)`` already names a
    stored binding, rather than silently overwriting the first -- AND refuses a
    re-insert under an existing ``binding_id`` whose domain fields changed (an
    illegal mutation of the immutable identity, not an update). Uniqueness is
    enforced on the DOMAIN, independent of ``binding_id``: a caller cannot slip
    past it by reusing an id and changing the account, nor by minting a new id for
    an account that is already bound. Downstream ACL keys on this domain, so it
    must be decidable and single-valued. The message names the domain, never a
    secret value.
    """


class BindingRevokedError(Exception):
    """Raised when a credential/handle is fenced (revoked, stale, or forged).

    :meth:`BindingStore.assert_live` and :meth:`BindingStore.resolve` require a
    presented handle's ``generation`` to EXACTLY equal the store's current live
    generation for the SAME binding identity. This fires when the identity is
    fine but the generation is stale (older than live), forged (a future value the
    store never issued), revoked, or when the presented generation is numerically
    valid but belongs to a DIFFERENT binding. Distinct from
    :class:`BindingResolutionError` (no/ambiguous match) and
    :class:`~kiro_crew.connections.control_plane.binding.BindingVerificationError`
    (the claim did not verify). The message names neither a secret value nor
    another principal.
    """


class SecretReader(Protocol):
    """Reads a secret VALUE by its vault-entry name.

    The one dependency :meth:`BindingStore.select_secret` needs from the secret
    subsystem, expressed as a Protocol so this module does NOT create a vault or
    hard-wire ``config_dir`` into a ``SecretVault`` construction -- the existing
    :class:`kiro_crew.secrets.SecretVault` satisfies it directly (its ``get(name)``
    returns a ``SecretValue`` whose ``.reveal()`` yields the plaintext, or
    ``None`` when absent). Injecting it keeps L04 free of a second vault and keeps
    the selector testable with a fake reader.
    """

    def get(self, name: str) -> "object | None":  # pragma: no cover - Protocol
        ...


class ResolvedCredential(TypedDict):
    """What :meth:`BindingStore.select_secret` returns: the LIVE, trusted answer.

    Every field comes from the trusted live store record (never from a
    caller-supplied dict): ``binding_id`` and ``generation`` identify the exact
    live binding the secret was resolved for, ``credential_mode`` is the store's
    mode (what the sender authenticates AS), ``secret_ref`` is the store's
    per-binding reference (the vault-entry NAME + metadata), and ``secret`` is the
    ``SecretValue`` the reader returned for that name. The sender consumes THIS
    envelope; it never re-derives any of these from its own inputs.
    """

    binding_id: str
    generation: int
    credential_mode: CredentialMode
    secret_ref: SecretRef
    secret: object  # a kiro_crew.secrets.SecretValue (opaque; .reveal() for plaintext)


def _account_key(*, service_id: str, subject_ref: str, tenant_ref: str) -> tuple[str, str, str]:
    """The ACCOUNT half of the uniqueness domain: (service, subject, tenant).

    This is what "a provider account" means to the downstream ACL's
    ``resolve(principal, provider, account)``: the neutral service range plus the
    VERIFIED subject and tenant. Compared as an exact tuple of the three stored
    reference strings -- no normalization, no case folding -- because the refs are
    already the verifier's canonical output, and folding here would let two
    verifier-distinct identities collide.
    """

    return (service_id, subject_ref, tenant_ref)


def _uniqueness_key(stored: StoredBinding) -> tuple[str, str, str, str]:
    """The FULL uniqueness domain: (deployment, service, subject, tenant).

    ``deployment_id`` (a PROVIDER-side deployment) prefixes the account key so the
    same provider account on two different provider deployments is two distinct
    bindings, and one deployment reached from two Kiro instances is still ONE
    binding. Two stored bindings are "the same binding" iff this 4-tuple is equal;
    an insert colliding on it is refused. Decidable from stored fields alone.
    """

    b = stored["binding"]
    return (
        stored["deployment_id"],
        *_account_key(
            service_id=b["service_id"],
            subject_ref=b["subject_ref"],
            tenant_ref=b["tenant_ref"],
        ),
    )


class BindingStore:
    """A trusted, persistent store of bindings with lifecycle operations.

    Every mutation is a reload-merge-publish under a cross-process lock, mirroring
    ``acp/seed_provenance.py``: the store file is shared by every Crew process on
    the host and ``atomic_write`` makes the last writer win outright, so a writer
    that did not first reload would drop a sibling's records. Reads
    (:meth:`resolve`, :meth:`get`) are lock-free point-in-time snapshots of the
    file; every WRITE (:meth:`insert`, :meth:`rotate`, :meth:`revoke`) reloads
    under the lock before publishing.

    The store keys records by ``binding_id`` on disk (the random, unguessable id
    L02 mints), and enforces the ``(deployment, account)`` uniqueness domain on
    insert. It is IO-doing by design -- that is the whole point of L04 -- but it
    persists only a binding RECORD, never a secret value.
    """

    def __init__(self, path: Path | None = None) -> None:
        # Default to the shared per-install store; a test may point at its own
        # file. The lock sits beside whichever store path is in play.
        self._path = Path(path) if path is not None else store_path()

    # --- paths -------------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def _lock_file(self) -> Path:
        return self._path.with_name(_LOCK_FILENAME)

    def _store_dir(self) -> Path:
        return self._path.parent

    # --- lock --------------------------------------------------------------

    def _cross_process_lock(self) -> "_LockCtx":
        return _locked(self._lock_file())

    # --- disk --------------------------------------------------------------

    def _read(self) -> dict[str, StoredBinding]:
        """Load ``{binding_id: StoredBinding}`` from disk.

        Distinguishes ABSENT from CORRUPT, which is the whole fix for the
        "corrupt store treated as empty" defect:

        * a file that does not exist -> ``{}`` (a genuinely empty store);
        * a zero-length / whitespace-only file -> :class:`BindingStoreCorruptError`
          (``atomic_write`` never produces a valid zero-length store, so an empty
          file is a torn write or tampering, NOT "no bindings");
        * a file that exists but is not valid JSON, or whose top level is not the
          expected ``{"bindings": {...}}`` shape -> :class:`BindingStoreCorruptError`
          (fail-closed: never silently empty);
        * a file that parses but carries even ONE malformed RECORD ->
          :class:`BindingStoreCorruptError` for the WHOLE store. A malformed row
          is NOT silently dropped: because ``_read`` feeds ``insert`` / ``rotate``
          / ``revoke``, which republish the whole map, a dropped record would be
          PERMANENTLY lost -- exactly the corruption outcome this fence exists to
          prevent -- so the whole store fails closed rather than serve/republish a
          silently-truncated view.

        A point-in-time snapshot. Callers that WRITE re-read this INSIDE the lock
        so the merge is against the current file, not a stale copy.
        """

        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            # Present but unreadable (permissions, IO error) -- not "empty".
            raise BindingStoreCorruptError(
                f"binding store at {self._path} is unreadable: {exc}"
            ) from exc
        if raw.strip() == "":
            # A zero-byte / whitespace-only file is NOT an empty store: publishing
            # goes through ``atomic_write`` (write-temp + fsync + rename), which
            # can never leave a VALID store zero-length -- a populated store always
            # serialises to ``{"schema_version": N, "bindings": {...}}``. So an
            # empty file is a torn write or external tampering, not "no bindings".
            # Treating it as ``{}`` would let the next mutation republish an empty
            # store over real records; fail closed instead.
            raise BindingStoreCorruptError(
                f"binding store at {self._path} is empty/zero-length, which "
                "atomic_write never produces for a populated store -- treating as "
                "tampered/torn rather than an empty store"
            )
        try:
            doc = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise BindingStoreCorruptError(
                f"binding store at {self._path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("bindings"), dict):
            raise BindingStoreCorruptError(
                f"binding store at {self._path} has an unexpected top-level shape"
            )
        records = doc["bindings"]
        out: dict[str, StoredBinding] = {}
        for bid, rec in records.items():
            if not _well_formed(rec):
                # A single malformed RECORD inside otherwise-valid JSON must NOT be
                # silently dropped: _read feeds insert/rotate/revoke, which
                # republish the WHOLE map, so a dropped record is PERMANENTLY lost
                # (the very outcome the corrupt-store fence exists to prevent). A
                # bad row means the file is not trustworthy as a whole, so fail
                # closed on the entire store rather than serve/republish a
                # silently-truncated view.
                raise BindingStoreCorruptError(
                    f"binding store at {self._path} carries a malformed record for "
                    f"{str(bid)!r}; refusing the whole store fail-closed rather "
                    "than dropping it (a dropped record is republished away and "
                    "permanently lost)"
                )
            out[str(bid)] = rec  # type: ignore[assignment]
        return out

    def _publish(self, records: dict[str, StoredBinding]) -> None:
        """Atomically write the whole store. MUST be called under the lock.

        ``mode=0o600``: the store names the provider accounts this install has
        bound. ``restrict_to_owner`` is deliberately NOT set -- the file holds no
        secret value (only references and metadata), so the owner-only lockdown a
        credential file needs would be over-claiming; ``0o600`` is the same
        posture the sibling ``seed_provenance`` sidecar uses for a
        reference-bearing file.
        """

        self._store_dir().mkdir(parents=True, exist_ok=True)
        doc = {"schema_version": LIFECYCLE_SCHEMA_VERSION, "bindings": records}
        atomic_write(self._path, json.dumps(doc), mode=0o600)

    # --- reads (lock-free snapshots) --------------------------------------

    def get(self, binding_id: str) -> StoredBinding | None:
        """The stored binding for ``binding_id``, or ``None``. Lock-free read.

        Raises :class:`BindingStoreCorruptError` if the store is unreadable rather
        than answering ``None`` for a corrupt file.
        """

        return self._read().get(binding_id)

    def all_bindings(self) -> list[StoredBinding]:
        """Every stored binding. Lock-free point-in-time snapshot."""

        return list(self._read().values())

    def resolve(
        self,
        *,
        kiro_principal: str,
        deployment_id: str,
        service_id: ServiceId,
        claimed_subject: str,
        claimed_tenant: str,
        verifier: SubjectTenantVerifier,
    ) -> Binding:
        """Resolve the binding a principal owns, FROM THE TRUSTED STORE.

        This is the ``resolve(principal, provider, account)`` seam the downstream
        ACL calls, re-founded on a trusted source: the candidate set is the
        PERSISTED store's own records, never an ``Iterable`` the caller passed in,
        so a caller cannot smuggle a binding it does not own into the
        candidate set.

        It enforces, in order:

        1. the VERIFIED provider identity -- runs ``verifier`` over the
           ``claimed_*`` inputs and matches on the returned ``subject_ref`` /
           ``tenant_ref`` plus ``service_id``;
        2. the ``deployment_id`` (provider-side deployment) scope;
        3. the ``kiro_principal`` link -- the caller's authenticated Kiro
           principal must EQUAL the one the stored binding is authorized for; a
           different principal does not resolve even with a correct provider
           identity (the real principal->authorization wiring, not a doc claim).

        A revoked binding is fenced: a verified, matching principal still resolves
        to nothing once revoked.

        Raises
        :class:`~kiro_crew.connections.control_plane.binding.BindingVerificationError`
        if the claim does not verify; :class:`BindingResolutionError`, fail-closed,
        on no match or (unreachable given insert-time uniqueness) more than one;
        :class:`BindingRevokedError` if the sole match is revoked;
        :class:`BindingStoreCorruptError` if the store is unreadable.
        """

        verified = verifier(
            claimed_subject=claimed_subject,
            claimed_tenant=claimed_tenant,
            service_id=service_id,
        )
        subject_ref = verified["subject_ref"]
        tenant_ref = verified["tenant_ref"]

        matches = [
            s
            for s in self._read().values()
            if s["deployment_id"] == deployment_id
            and s["kiro_principal"] == kiro_principal
            and s["binding"]["service_id"] == service_id
            and s["binding"]["subject_ref"] == subject_ref
            and s["binding"]["tenant_ref"] == tenant_ref
        ]
        if not matches:
            raise BindingResolutionError(
                "no stored binding for the verified principal on service "
                f"{service_id!r} deployment {deployment_id!r} for the caller principal"
            )
        if len(matches) > 1:
            # The uniqueness domain is enforced on insert, so a live store cannot
            # reach here; refuse fail-closed rather than guess if it ever does.
            raise BindingResolutionError(
                f"ambiguous: {len(matches)} stored bindings match on service "
                f"{service_id!r} deployment {deployment_id!r}"
            )
        stored = matches[0]
        if stored["revoked"]:
            raise BindingRevokedError(f"binding {stored['binding']['binding_id']!r} is revoked")
        # Return the binding stamped with the store's CURRENT live generation, so
        # a caller that carries the returned record forward is holding a live
        # handle, never a pre-revoke one.
        return _binding_at_generation(stored["binding"], stored["live_generation"])

    def resolve_for_acl(
        self, *, kiro_principal: str, deployment_id: str, service_id: ServiceId
    ) -> Binding | None:
        """Resolve the live binding a principal holds on ONE (deployment, service).

        This is the store-side lookup the ACL :class:`BindingResolver` adapter
        drives (see :mod:`kiro_crew.connections.control_plane.acl_binding_resolver`).
        It differs from :meth:`resolve` in ONE deliberate way: it takes NO
        caller-claimed subject/tenant and runs NO verifier, because the ACL
        contract hands only ``(principal, provider, account)`` -- there is no
        claimed identity to verify. The subject/tenant it returns are the ones
        the store ALREADY holds for this binding, which the verifier produced at
        insert time (:func:`create_binding` / :meth:`insert` store ONLY a
        verifier's :class:`VerifiedIdentity`). So the tenant a caller ultimately
        sees is a VERIFIED provider tenant read from the trusted store, never a
        value the caller asserted -- the "trusted tenant" discipline.

        The candidate set is the trusted PERSISTENT store, never a caller
        ``Iterable``; the match is keyed on the uniqueness domain's
        principal-facing axes ``(kiro_principal, deployment_id, service_id)``.

        Returns the matched binding stamped with the store's CURRENT live
        generation, or ``None`` -- FAIL-CLOSED -- when the principal holds no
        binding for this (deployment, service) or the sole match is revoked. It
        never falls back to another principal's or another deployment's binding.
        Raises :class:`BindingStoreCorruptError` if the store is unreadable (a
        corrupt store is NOT read as "no binding": failing closed to ``None`` on
        corruption would silently deny every candidate AND could later be papered
        over by a write, so corruption is surfaced, not swallowed).
        """

        matches = [
            s
            for s in self._read().values()
            if s["deployment_id"] == deployment_id
            and s["kiro_principal"] == kiro_principal
            and s["binding"]["service_id"] == service_id
        ]
        if not matches:
            return None
        if len(matches) > 1:
            # Insert enforces the full uniqueness domain, so a live store cannot
            # hold two records matching these three axes for one principal; if it
            # ever does, deny fail-closed rather than pick one.
            return None
        stored = matches[0]
        if stored["revoked"]:
            return None
        return _binding_at_generation(stored["binding"], stored["live_generation"])

    def resolve_deployment_for_account(self, *, service_id: ServiceId, account: str) -> str | None:
        """Map an ACL ``(service_id, account)`` to the ``deployment_id`` hosting it.

        This is the TRUSTED, STORE-BACKED account -> deployment mapping the ACL
        adapter's production default uses. The source of truth is the persisted
        binding store itself: a binding was inserted with the vendor ``account``
        it is for AND the ``deployment_id`` that hosts it, so the store already
        records the pairing -- no separate registry, no caller-supplied map, no
        fixture lambda.

        It matches stored records on ``(service_id, account)`` and returns the
        DISTINCT ``deployment_id`` they share. Fail-closed to ``None`` when:

        * no stored binding names this ``(service_id, account)`` -- unknown
          account, deny;
        * records exist under ``(service_id, account)`` on MORE THAN ONE
          ``deployment_id`` -- the SAME account NAME hosted on two different
          provider deployments. ``(provider, account)`` alone CANNOT disambiguate
          these, so this resolver refuses rather than guess. Disambiguating them
          needs an endpoint/host discriminator the ACL's
          ``resolve(principal, provider, account)`` does not carry today -- a
          NAMED interface gap (see the ACL adapter module docstring), not a thing
          to paper over by picking one.

        Records inserted WITHOUT an ``account`` (older envelopes) do not
        participate -- they cannot answer an account query, so they are skipped.
        Raises :class:`BindingStoreCorruptError` if the store is unreadable.
        """

        deployments = {
            s["deployment_id"]
            for s in self._read().values()
            if s.get("account") == account and s["binding"]["service_id"] == service_id
        }
        if len(deployments) != 1:
            # 0 = unknown account (deny); >1 = same account name on multiple
            # deployments, unresolvable from (provider, account) alone (deny).
            return None
        return next(iter(deployments))

    def resolve_deployment_for_account_endpoint(
        self, *, service_id: ServiceId, account: str, endpoint: str
    ) -> str | None:
        """Map ``(service_id, account, endpoint)`` to its hosting ``deployment_id``.

        This is the DISCRIMINATED account -> deployment lookup the ``resolve_ref``
        path uses when the ref carries an endpoint/host (a Salesforce
        ``instanceUrl``, a Graph endpoint). It is what disambiguates the SAME
        account name on two different provider deployments that
        :meth:`resolve_deployment_for_account` must refuse.

        The store is the SOLE AUTHORITY on which endpoints exist: the match is
        over stored records that recorded THIS ``endpoint`` (a caller-supplied
        endpoint that no stored binding registered simply does not match, so an
        unregistered endpoint returns ``None`` -- deny, never trust the caller's
        endpoint on its face). Fail-closed to ``None`` for an unknown
        ``(service, account, endpoint)`` triple AND for the (should-not-happen,
        given insert uniqueness) case of more than one deployment on it. Records
        without a stored ``endpoint`` do not participate. Raises
        :class:`BindingStoreCorruptError` if the store is unreadable.
        """

        deployments = {
            s["deployment_id"]
            for s in self._read().values()
            if s.get("account") == account
            and s.get("endpoint") == endpoint
            and s["binding"]["service_id"] == service_id
        }
        if len(deployments) != 1:
            return None
        return next(iter(deployments))

    # --- writes (reload-merge-publish under the lock) ---------------------

    def insert(
        self,
        binding: Binding,
        *,
        deployment_id: str,
        kiro_principal: str,
        account: str | None = None,
        endpoint: str | None = None,
    ) -> StoredBinding:
        """Admit ``binding`` into the trusted store, or REFUSE.

        Enforces the ``(deployment, account)`` uniqueness domain ON THE DOMAIN,
        independent of ``binding_id``:

        * if a stored binding already has the same
          ``(deployment_id, service_id, subject_ref, tenant_ref)`` -> refuse
          (:class:`BindingUniquenessError`), never a silent overwrite -- even
          though the new record carries a different random ``binding_id``;
        * if a record already exists under THIS ``binding_id`` but its stored
          domain fields differ from the incoming binding's -> refuse as an illegal
          mutation of an immutable identity (a caller cannot reuse an id and change
          the account to slip past the domain check);
        * a byte-identical re-insert under the same id and same domain is the only
          idempotent case, and it too does not overwrite lifecycle state. "Byte
          identical" is scoped EXPLICITLY (see :func:`_same_stored_binding`): the
          embedded binding's ``binding_id``, ``service_id``, ``subject_ref``,
          ``tenant_ref``, ``credential_mode``, ``generation`` and
          ``secret_ref['name']`` + ``secret_ref['backend']``, PLUS the record's
          ``deployment_id`` and ``kiro_principal``. Volatile wall-clock stamps
          (``created_at`` / ``bound_at`` / ``updated_at``) are DELIBERATELY
          excluded, since they differ per :func:`create_binding` call. If any
          compared field differs -> refuse (:class:`BindingUniquenessError`), never
          a silent ``return prior`` of the old record.

        Reload-merge-publish under the cross-process lock, so a concurrent insert
        on the same host cannot both win. Fails closed on a corrupt store
        (:class:`BindingStoreCorruptError`) rather than treating it as empty and
        erasing it.

        ``deployment_id`` is a PROVIDER-side deployment; ``kiro_principal`` is the
        authenticated Kiro principal authorized to use the binding. The stored
        record's ``live_generation`` starts at the binding's own ``generation``.
        """

        candidate: StoredBinding = {
            "binding": binding,
            "deployment_id": deployment_id,
            "kiro_principal": kiro_principal,
            "live_generation": binding["generation"],
            "revoked": False,
            "updated_at": time.time(),
        }
        if account is not None:
            candidate["account"] = account
        if endpoint is not None:
            candidate["endpoint"] = endpoint
        want = _uniqueness_key(candidate)
        bid = binding["binding_id"]
        with self._cross_process_lock():
            records = self._read()  # fail-closed on corrupt
            # Illegal-mutation guard: an existing record under this id whose domain
            # changed is a rewrite of an immutable identity, not an update.
            prior = records.get(bid)
            if prior is not None and _uniqueness_key(prior) != want:
                raise BindingUniquenessError(
                    f"binding id {bid!r} already names a different account "
                    "(deployment/service/subject/tenant changed); refusing to "
                    "mutate an immutable binding identity"
                )
            # Domain-uniqueness guard: some OTHER id already holds this account.
            for other_id, existing in records.items():
                if other_id != bid and _uniqueness_key(existing) == want:
                    raise BindingUniquenessError(
                        "a binding already exists on the uniqueness domain "
                        f"(deployment={deployment_id!r}, service="
                        f"{binding['service_id']!r}, subject/tenant verified); "
                        "refusing to overwrite"
                    )
            if prior is not None:
                # A same-id + same-domain re-insert is idempotent ONLY when it is
                # byte-identical over the explicitly-scoped field set below;
                # otherwise it is a conflicting rewrite (changed principal /
                # credential_mode / secret_ref) and must be refused, never a
                # silent return of the OLD record.
                if not _same_stored_binding(prior, candidate):
                    raise BindingUniquenessError(
                        f"binding id {bid!r} already exists with different content "
                        "(principal, credential_mode, or secret_ref differ); refusing "
                        "to silently keep the old record on a conflicting re-insert"
                    )
                # Byte-identical re-insert: keep the existing lifecycle state
                # (generation/revoked/updated_at) untouched.
                return prior
            records[bid] = candidate
            self._publish(records)
        return candidate

    def rotate(
        self,
        binding_id: str,
        *,
        observed_generation: int,
        refresh: "Callable[[Binding], SecretRef | None] | None" = None,
    ) -> "tuple[StoredBinding, bool]":
        """Refresh-rotate a binding as the CROSS-PROCESS SINGLE WRITER.

        A rotation advances the binding's generation (via L02's
        :func:`next_generation` semantics). ``refresh`` is the caller's REAL
        token-refresh work -- the call that actually hits the provider's token
        endpoint and returns the rotated :class:`SecretRef` (or ``None`` to keep
        the current reference). **It is invoked INSIDE the cross-process lock**, so
        under contention the provider endpoint is hit EXACTLY ONCE, not once per
        process. It NEVER touches a secret value; the returned reference points at
        the vault entry a later leaf writes.

        Returns ``(record, rotated_by_this_call)``. ``rotated_by_this_call`` is
        ``True`` iff THIS call performed the rotation (and thus called
        ``refresh``), and ``False`` when it short-circuited because a concurrent
        writer had already rotated -- in which case ``refresh`` is NOT called.

        **Single-writer under contention.** ``observed_generation`` is the
        generation the caller saw BEFORE trying to rotate. Two processes racing a
        refresh both pass the same observed generation. The lock serializes them;
        the winner rotates, calls ``refresh`` once, advances ``live_generation``,
        and returns ``(record, True)``; the LOSER, on acquiring the lock, re-reads,
        finds ``live_generation`` differs from its ``observed_generation``, and
        returns ``(winner_record, False)`` WITHOUT calling ``refresh`` or rotating.

        Raises :class:`BindingResolutionError` if the id is unknown,
        :class:`BindingRevokedError` if the binding is revoked (a revoked binding
        is not rotated), and :class:`BindingStoreCorruptError` on a corrupt store.
        """

        with self._cross_process_lock():
            records = self._read()  # fail-closed on corrupt
            stored = records.get(binding_id)
            if stored is None:
                raise BindingResolutionError(f"no stored binding with id {binding_id!r} to rotate")
            if stored["revoked"]:
                raise BindingRevokedError(f"binding {binding_id!r} is revoked; not rotating")
            # Single-writer short-circuit: if the live generation already moved
            # past what this caller observed, someone else rotated while we waited
            # for the lock. Do NOT call refresh, do NOT rotate; return their result.
            if stored["live_generation"] != observed_generation:
                return stored, False

            # We are the single writer. The REAL refresh runs HERE, under the lock,
            # so the provider token endpoint is hit exactly once under contention.
            new_secret_ref: SecretRef | None = None
            if refresh is not None:
                new_secret_ref = refresh(stored["binding"])

            rotated_binding = next_generation(stored["binding"])
            if new_secret_ref is not None:
                rotated_binding = dict(rotated_binding)  # type: ignore[assignment]
                rotated_binding["secret_ref"] = new_secret_ref
            updated: StoredBinding = {
                "binding": rotated_binding,
                "deployment_id": stored["deployment_id"],
                "kiro_principal": stored["kiro_principal"],
                "live_generation": rotated_binding["generation"],
                "revoked": False,
                "updated_at": time.time(),
            }
            # Carry the vendor account forward: it is lifecycle provenance the
            # store-backed account -> deployment mapping reads, and dropping it on
            # rotate would silently break account resolution for a rotated binding.
            if "account" in stored:
                updated["account"] = stored["account"]
            if "endpoint" in stored:
                updated["endpoint"] = stored["endpoint"]
            records[binding_id] = updated
            self._publish(records)
            return updated, True

    def revoke(self, binding_id: str) -> StoredBinding:
        """Revoke a binding: raise its generation and fence every other one.

        Marks the binding ``revoked`` and advances ``live_generation`` (L02 left
        the generation counter here for exactly this). After this returns, any
        credential/handle whose generation is not EXACTLY the new live generation
        (for this identity) is refused by :meth:`assert_live` and :meth:`resolve`
        -- and a revoked binding is refused regardless of generation.
        Reload-merge-publish under the cross-process lock. Idempotent: revoking an
        already-revoked binding is a no-op that still returns the record.

        Raises :class:`BindingResolutionError` if the id is unknown, and
        :class:`BindingStoreCorruptError` on a corrupt store.
        """

        with self._cross_process_lock():
            records = self._read()  # fail-closed on corrupt
            stored = records.get(binding_id)
            if stored is None:
                raise BindingResolutionError(f"no stored binding with id {binding_id!r} to revoke")
            if stored["revoked"]:
                return stored
            revoked_binding = next_generation(stored["binding"])
            updated: StoredBinding = {
                "binding": revoked_binding,
                "deployment_id": stored["deployment_id"],
                "kiro_principal": stored["kiro_principal"],
                "live_generation": revoked_binding["generation"],
                "revoked": True,
                "updated_at": time.time(),
            }
            if "account" in stored:
                updated["account"] = stored["account"]
            if "endpoint" in stored:
                updated["endpoint"] = stored["endpoint"]
            records[binding_id] = updated
            self._publish(records)
            return updated

    # --- fencing -----------------------------------------------------------

    def assert_live(self, binding: Binding) -> Binding:
        """Refuse ``binding`` unless it is the store's live handle, and RETURN the
        store's trusted record for it.

        The fencing gate a credential/handle passes through before use. Reads the
        store's CURRENT record for ``binding['binding_id']`` and refuses
        (:class:`BindingRevokedError`) unless ALL of these hold:

        * the ``binding_id`` is known to the store (never admitted -> refuse);
        * the store's record for it is NOT revoked;
        * the record's IDENTITY matches the presented handle -- same
          ``service_id`` / ``subject_ref`` / ``tenant_ref`` (a handle carrying
          another binding's fields, or another binding's numerically-valid
          generation, is refused);
        * the presented ``generation`` EQUALS the store's live generation for that
          binding -- not "less than or equal", not "not greater". A STALE
          pre-revoke generation is refused, and a FORGED future generation (e.g.
          ``999`` the store never issued) is refused for the same reason: only the
          exact current generation is live;
        * the presented ``credential_mode`` EQUALS the store's record -- a handle
          that keeps the right id/identity/generation but swaps the mode (e.g.
          ``oauth_user`` -> ``service_to_service``) is refused;
        * the presented ``secret_ref`` EQUALS the store's record (compared as the
          whole reference dict) -- a handle that swaps the secret reference (e.g.
          points its ``name`` at another vault entry) is refused.

        This is what makes ``generation`` load-bearing: neither an old handle nor a
        fabricated future one can authenticate.

        **The real sender MUST consume the RETURNED value** for the ref/mode/secret
        it uses, and must NEVER re-read the caller-supplied ``binding`` dict after
        this call: the return is the exact live record from the trusted store
        (``_binding_at_generation(stored['binding'], stored['live_generation'])``),
        so a value that agreed at the fence cannot be swapped out afterward. This
        closes the "validate then use an unvalidated value" gap -- assert_live no
        longer hands back ``None`` and force the caller to fall back to its own
        (untrusted) dict.

        Raises :class:`BindingStoreCorruptError` on a corrupt store.
        """

        bid = binding["binding_id"]
        stored = self._read().get(bid)
        if stored is None:
            raise BindingRevokedError(f"binding {bid!r} is not in the trusted store")
        if stored["revoked"]:
            raise BindingRevokedError(f"binding {bid!r} is revoked")
        sb = stored["binding"]
        if (
            binding["service_id"] != sb["service_id"]
            or binding["subject_ref"] != sb["subject_ref"]
            or binding["tenant_ref"] != sb["tenant_ref"]
        ):
            raise BindingRevokedError(
                f"binding {bid!r} identity does not match the trusted store's record"
            )
        if binding["generation"] != stored["live_generation"]:
            raise BindingRevokedError(
                f"binding {bid!r} generation {binding['generation']} is fenced; the "
                f"only live generation is {stored['live_generation']}"
            )
        if binding["credential_mode"] != sb["credential_mode"]:
            raise BindingRevokedError(
                f"binding {bid!r} credential_mode does not match the trusted store's record"
            )
        if binding["secret_ref"] != sb["secret_ref"]:
            raise BindingRevokedError(
                f"binding {bid!r} secret_ref does not match the trusted store's record"
            )
        # Return the TRUSTED live record, never the caller's dict: the sender must
        # consume THIS for ref/mode/secret. Stamped with the live generation (it
        # already equals the presented one, but this keeps the invariant explicit).
        return _binding_at_generation(sb, stored["live_generation"])

    # --- the per-binding secret selector (live-store trusted source) ------

    def select_secret(
        self,
        binding: Binding,
        *,
        reader: SecretReader,
    ) -> ResolvedCredential:
        """Resolve the secret for THIS binding FROM THE TRUSTED LIVE STORE.

        This is the per-binding selector L09's payload path calls. It resolves the
        secret reference **by binding, from the live store** -- NOT by provider
        slug (the legacy :func:`binding_secret_ref` collapses every binding under a
        provider onto one name), and NOT from any candidate set the caller passed
        in. The sixth recurrence of the trusted-source discipline, closed the same
        way as the previous five: the value used comes from the store, never from
        the caller's own copy.

        In order:

        1. **Fence against the LIVE store.** Calls :meth:`assert_live`, which
           re-reads the store's CURRENT record for ``binding['binding_id']`` and
           refuses (:class:`BindingRevokedError`) a revoked binding, a stale or
           forged generation, or a swapped identity / ``credential_mode`` /
           ``secret_ref``. So a rotated, revoked, or tampered handle cannot pull a
           secret. ``assert_live`` RETURNS the trusted live binding.
        2. **Take the secret reference from the STORE's record**, i.e. the
           ``secret_ref`` on the value ``assert_live`` returned -- never
           ``binding['secret_ref']`` as the caller supplied it. (They agreed at the
           fence, but the selector consumes the store's copy on principle, so no
           unvalidated caller value is ever the one used.)
        3. **Read the value by that trusted name** through ``reader`` (the existing
           :class:`~kiro_crew.secrets.SecretVault` or any :class:`SecretReader`).
           A name the reader does not hold raises :class:`BindingResolutionError`
           -- the binding names a secret that is not in the vault.

        Returns a :class:`ResolvedCredential` whose every field is the store's
        (binding_id, generation, credential_mode, secret_ref, secret). The sender
        consumes THIS envelope and re-derives nothing from its own inputs.

        Raises :class:`BindingRevokedError` (fenced), :class:`BindingResolutionError`
        (secret absent), or :class:`BindingStoreCorruptError` (corrupt store).
        """

        live = self.assert_live(binding)  # reads the LIVE store; returns trusted record
        trusted_ref = live["secret_ref"]  # the STORE's ref, not the caller's
        value = reader.get(trusted_ref["name"])
        if value is None:
            raise BindingResolutionError(
                f"binding {live['binding_id']!r} names secret "
                f"{trusted_ref['name']!r} which is not in the vault"
            )
        return {
            "binding_id": live["binding_id"],
            "generation": live["generation"],
            "credential_mode": live["credential_mode"],
            "secret_ref": trusted_ref,
            "secret": value,
        }


def _binding_at_generation(binding: Binding, generation: int) -> Binding:
    """A copy of ``binding`` stamped with ``generation`` (does not mutate input)."""

    out: Binding = dict(binding)  # type: ignore[assignment]
    out["generation"] = generation
    return out


def _same_stored_binding(prior: StoredBinding, candidate: StoredBinding) -> bool:
    """Whether two stored records are byte-identical for idempotent-re-insert.

    The comparison scope is EXPLICIT and stable, so an idempotent re-insert is
    decidable without tripping on wall-clock noise:

    * from the embedded binding: ``binding_id``, ``service_id``, ``subject_ref``,
      ``tenant_ref``, ``credential_mode``, ``generation`` and the secret
      REFERENCE's ``name`` + ``backend`` (its LOCATION, not its ``bound_at``
      stamp);
    * from the lifecycle envelope: ``deployment_id`` and ``kiro_principal``.

    Volatile timestamps (``created_at`` on the binding, ``bound_at`` on the
    secret ref, ``updated_at`` on the record) are DELIBERATELY excluded: they
    differ per :func:`create_binding` call and would make a genuine idempotent
    re-insert look like a conflict. Everything a re-insert could meaningfully
    CHANGE (principal, mode, the secret's location) is inside the compared set,
    so a differing re-insert is refused rather than silently keeping the old
    record. ``live_generation`` / ``revoked`` are lifecycle STATE the store owns,
    not caller-supplied on insert, so they are not part of the identity compare.
    """

    pb = prior["binding"]
    cb = candidate["binding"]
    for field in (
        "binding_id",
        "service_id",
        "subject_ref",
        "tenant_ref",
        "credential_mode",
        "generation",
    ):
        if pb.get(field) != cb.get(field):
            return False
    if pb["secret_ref"].get("name") != cb["secret_ref"].get("name"):
        return False
    if pb["secret_ref"].get("backend") != cb["secret_ref"].get("backend"):
        return False
    if prior["deployment_id"] != candidate["deployment_id"]:
        return False
    if prior["kiro_principal"] != candidate["kiro_principal"]:
        return False
    # The vendor account is part of the record's identity for idempotency: a
    # re-insert (or a rotate/revoke rewrite) that DROPPED or CHANGED it must NOT
    # compare equal, or the byte-identical check would mask exactly the "lost
    # account" defect. ``.get`` so a legacy record without the key compares equal
    # to another legacy record without it, but differs from one that carries it.
    if prior.get("account") != candidate.get("account"):
        return False
    if prior.get("endpoint") != candidate.get("endpoint"):
        return False
    return True


def _well_formed(rec: object) -> bool:
    """Whether an on-disk record has the :class:`StoredBinding` shape.

    Fail-closed and COMPLETE: it validates EVERY field a downstream consumer
    reads, not just that a top-level key is present, because a half-typed record
    that passes here is later dereferenced and CRASHES. In particular a
    ``secret_ref`` of ``null`` (or a non-dict, or missing its ``name`` /
    ``backend``) would pass a shallow check, then :meth:`BindingStore.resolve`
    -> :meth:`BindingStore.select_secret` reads ``secret_ref["name"]`` and raises
    ``TypeError``/``KeyError``. So the embedded ``secret_ref`` is validated as a
    nested object, and ``credential_mode`` (which :meth:`assert_live` compares)
    is validated too. ``account`` is optional (``NotRequired``) so it is checked
    only WHEN PRESENT.
    """

    if not isinstance(rec, dict):
        return False
    if not isinstance(rec.get("deployment_id"), str):
        return False
    if not isinstance(rec.get("kiro_principal"), str):
        return False
    if not isinstance(rec.get("live_generation"), int) or isinstance(
        rec.get("live_generation"), bool
    ):
        return False
    if not isinstance(rec.get("revoked"), bool):
        return False
    # account is NotRequired; when present it must be a str (it keys the
    # store-backed account -> deployment resolution).
    if "account" in rec and not isinstance(rec.get("account"), str):
        return False
    if "endpoint" in rec and not isinstance(rec.get("endpoint"), str):
        return False
    b = rec.get("binding")
    if not isinstance(b, dict):
        return False
    for field in ("binding_id", "service_id", "subject_ref", "tenant_ref", "credential_mode"):
        if not isinstance(b.get(field), str):
            return False
    if not isinstance(b.get("generation"), int) or isinstance(b.get("generation"), bool):
        return False
    # The secret REFERENCE is consumed by select_secret (secret_ref["name"]) and
    # compared by assert_live. It must be a dict with string name + backend --
    # NOT null, not a non-dict, not missing either key -- or a later dereference
    # crashes on an otherwise "valid" record.
    sr = b.get("secret_ref")
    if not isinstance(sr, dict):
        return False
    if not isinstance(sr.get("name"), str) or not isinstance(sr.get("backend"), str):
        return False
    return True


def _locked(lock_file: Path) -> "_LockCtx":
    """Context manager: hold an exclusive cross-process lock on ``lock_file``.

    Goes through :func:`platform_compat.acquire_lock` / ``release_lock`` -- NEVER
    a raw ``fcntl`` call, which would not serialize with the Windows msvcrt path
    the rest of the codebase relies on. FAILS CLOSED: a stuck holder past the
    ceiling raises rather than letting a writer proceed unserialized, so a lost
    write becomes a loud error, never a silent drop. The lock file is opened with
    ``O_CREAT | O_RDWR`` (never truncating), matching ``open_lock_file``.
    """

    return _LockCtx(lock_file)


class _LockCtx:
    """The cross-process lock context, via ``platform_compat`` (never raw fcntl)."""

    def __init__(self, lock_file: Path) -> None:
        self._lock_file = lock_file
        self._fd: int | None = None

    def __enter__(self) -> None:
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT | O_RDWR, never O_TRUNC: a contender must not observe the lock
        # file flicker empty (the GH-9248 discipline open_lock_file documents).
        self._fd = os.open(str(self._lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        # Exclusive, blocking (POSIX) / bounded-poll fail-closed (Windows).
        platform_compat.acquire_lock(self._fd, exclusive=True)

    def __exit__(self, *exc: object) -> None:
        fd = self._fd
        self._fd = None
        if fd is not None:
            try:
                platform_compat.release_lock(fd)
            finally:
                os.close(fd)
