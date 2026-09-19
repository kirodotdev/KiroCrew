"""Production factory: compose a :class:`DriveOperations` from W01's public seam.

This is the piece the host installs into
``app["knowledge_connector_runners"]["google_drive"]`` so the shared knowledge
handler's landed runner-injection seam can construct
``GoogleDriveConnector(operations_factory)`` with a REAL, executable runner
(rather than a no-arg dead registration, which its own ``validate_config``
refuses).

It composes ONLY W01's public control-plane entry points --
:meth:`~kiro_crew.connections.control_plane.lifecycle.BindingStore.resolve`,
:func:`~kiro_crew.connections.control_plane.handle.derive_handle` /
:func:`~kiro_crew.connections.control_plane.handle.ensure_usable`,
:class:`~kiro_crew.connections.control_plane.production.BindingCustodyGate`,
:func:`~kiro_crew.connections.control_plane.production.build_production_transport`,
and :func:`~kiro_crew.connections.control_plane.executor.execute` /
:class:`~kiro_crew.connections.control_plane.executor.PageWalk` -- plus this
package's own request assembly (:mod:`.locator`, :mod:`.decode`). There is NO
second auth, NO self-built sender, NO HTTP, NO vault and NO token here: credential
custody is single-sourced in W01. The Drive side only says WHICH operation with
WHICH args and reads the neutral outcome.

AUTHORIZATION IS NOT MINTED HERE. A sync must never CREATE authority as a side
effect -- doing so would let a REVOKED source be silently resurrected on the next
sync. This factory RESOLVES the already-trusted binding the host's authorization
flow admitted (:meth:`BindingStore.resolve`), which fences a revoked/absent
binding by RAISING (``BindingRevokedError`` / ``BindingResolutionError`` /
``BindingVerificationError``). Minting a binding (``create_binding`` +
``BindingStore.insert``) is the host's authorization flow, not the connector's,
and is deliberately absent from this module.

Everything that carries AUTHORITY is supplied BY THE HOST, never defaulted in
vendor code:

* ``granted_scopes`` -- the scopes the STORED grant actually carries. A default
  here would be the vendor asserting which scopes it holds; it must come from the
  real grant record. (``requested_scopes`` keeps a read-only minimization default
  -- narrowing is legitimate, and it is checked to be a subset of ``granted``.)
* ``layers`` -- the five-layer governance ceilings. A vendor-built empty
  :class:`~kiro_crew.connections.control_plane.policy.LayerCeilings` would bypass
  governance; the host passes its configured ceilings.
* ``clock`` -- a live time source, REQUIRED. A frozen instant would make every
  TTL / expiry / handle-validity check vacuous, so there is no ``now`` fallback.

Token custody addresses the credential by the RESOLVED BINDING's own scoped
``secret_ref`` (via W01's store + transport), never a provider-slug vault family:
per-binding custody must be real, not apparent.
"""

from __future__ import annotations

from typing import Callable, Mapping, Tuple

from kiro_crew.connections.control_plane.auth_modes import declare_permitted_modes
from kiro_crew.connections.control_plane.binding import Binding, SubjectTenantVerifier
from kiro_crew.connections.control_plane.executor import (
    ExecutionOutcome,
    PageWalk,
    execute,
)
from kiro_crew.connections.control_plane.handle import (
    DerivedHandle,
    derive_handle,
    ensure_usable,
)
from kiro_crew.connections.control_plane.lifecycle import BindingStore
from kiro_crew.connections.control_plane.operation import (
    CredentialMode,
    OperationDescriptor,
)
from kiro_crew.connections.control_plane.policy import LayerCeilings
from kiro_crew.connections.control_plane.production import (
    BindingCustodyGate,
    HttpSend,
    SecretStore,
    Transport,
    build_production_transport,
    urllib_http_send,
)

from . import decode as drive_decode
from . import locator as drive_locator
from .operations import DriveOperations, OperationRunner

#: Drive read operations authenticate as the querying/owning user's OAuth grant.
_CREDENTIAL_MODE: CredentialMode = "oauth_user"

#: The governed catalog scope a Drive operation is authorized under (a live
#: SCOPE_CATALOG member; "knowledge" is NOT one and is deny-by-default), with the
#: operation id as the governed item.
_GOVERNANCE_SCOPE = "tools"

#: The read scope a Drive sync REQUESTS. A minimization default only -- it is
#: narrowed against the host-supplied ``granted_scopes`` and must be a subset
#: (``derive_handle`` refuses a requested scope the grant does not carry). It is
#: NOT an assertion of authority: ``granted_scopes`` has no default and must be
#: the real stored grant.
_DEFAULT_REQUESTED_SCOPES: Tuple[str, ...] = ("https://www.googleapis.com/auth/drive.readonly",)


class _W01OperationRunner(OperationRunner):
    """An :class:`OperationRunner` that runs each Drive op through W01's executor.

    Holds the derived handle, the host's governance ceilings, and a per-descriptor
    transport composer. It NEVER touches a socket, a token, or the vault directly:
    ``execute`` / ``PageWalk`` drive the composed production transport, which is
    where W01 owns custody. The transport is composed PER descriptor because a
    decode is bound to one operation (W01 calls ``decode`` with the reply alone),
    so ``decode.for_operation`` selects the right shape for the op being run.

    ``layers`` is the host's five-layer governance ceilings, carried through to
    every ``execute`` / ``PageWalk`` -- the runner never fabricates an empty one.
    ``clock`` is the live time source re-read on every call and every page, so a
    handle that expires mid-walk stops the walk.
    """

    def __init__(
        self,
        *,
        handle: DerivedHandle,
        layers: LayerCeilings,
        transport_for: Callable[[OperationDescriptor], Transport],
        clock: Callable[[], float],
    ) -> None:
        self._handle = handle
        self._layers = layers
        self._transport_for = transport_for
        self._clock = clock

    def run(
        self, descriptor: OperationDescriptor, request_args: Mapping[str, object]
    ) -> ExecutionOutcome:
        return execute(
            descriptor,
            self._handle,
            self._transport_for(descriptor),
            now=self._clock(),
            offered_mode=_CREDENTIAL_MODE,
            permitted=declare_permitted_modes((_CREDENTIAL_MODE,)),
            layers=self._layers,
            governance_scope=_GOVERNANCE_SCOPE,
            governance_item=descriptor["operation_id"],
            request_args=dict(request_args),
        )

    def walk(self, descriptor: OperationDescriptor, base_args: Mapping[str, object]):
        walk = PageWalk(
            descriptor=descriptor,
            handle=self._handle,
            transport=self._transport_for(descriptor),
            offered_mode=_CREDENTIAL_MODE,
            permitted=declare_permitted_modes((_CREDENTIAL_MODE,)),
            layers=self._layers,
            governance_scope=_GOVERNANCE_SCOPE,
            governance_item=descriptor["operation_id"],
            clock=self._clock,
            base_args=dict(base_args),
        )
        while not walk.done:
            yield walk.next()


def build_drive_operations(
    *,
    subject: str,
    tenant: str,
    verifier: SubjectTenantVerifier,
    binding_store: BindingStore,
    vault: SecretStore,
    deployment_id: str,
    kiro_principal: str,
    granted_scopes: Tuple[str, ...],
    layers: LayerCeilings,
    clock: Callable[[], float],
    ttl_seconds: float,
    requested_scopes: Tuple[str, ...] = _DEFAULT_REQUESTED_SCOPES,
    page_size: int = 100,
    http_send: HttpSend = urllib_http_send,
) -> DriveOperations:
    """Compose one executable :class:`DriveOperations` for a single source identity.

    Steps, all on W01's public seam:

    1. ``binding_store.resolve`` RESOLVES the already-trusted binding the host's
       authorization flow admitted for this verified ``(subject, tenant)`` /
       ``deployment_id`` / ``kiro_principal``. It does NOT mint one: a revoked or
       absent binding RAISES (``BindingRevokedError`` / ``BindingResolutionError``
       / ``BindingVerificationError``), so a revoked source cannot be resurrected
       by a sync. The returned binding is stamped with the store's live generation.
    2. ``derive_handle`` narrows a short-lived handle to ``requested_scopes`` --
       proven a subset of the host-supplied ``granted_scopes`` (the real stored
       grant); a requested scope the grant does not carry raises, never widens.
    3. ``BindingCustodyGate`` fences that handle's identity; a call routed for a
       different binding is refused before any secret is read.
    4. ``build_production_transport`` composes the real transport (gate + store +
       vault + this package's ``locator`` + per-op ``decode``). The credential is
       resolved by W01 from the RESOLVED BINDING's own ``secret_ref`` -- not a
       provider-slug family. The runner drives ``execute`` / ``PageWalk`` over it,
       carrying the host's governance ``layers`` and re-reading ``clock`` each call.

    Every authority input (``granted_scopes``, ``layers``, ``clock``,
    ``ttl_seconds``) is REQUIRED and host-supplied; none is defaulted in vendor
    code. Raises whatever W01 raises on a bad identity/scope/revocation rather than
    swallowing it -- a source that cannot be bound must fail loudly, not produce a
    dead runner.
    """
    now = clock()

    binding: Binding = binding_store.resolve(
        kiro_principal=kiro_principal,
        deployment_id=deployment_id,
        service_id="google_drive",
        claimed_subject=subject,
        claimed_tenant=tenant,
        verifier=verifier,
    )
    handle = derive_handle(
        binding,
        granted_scopes=granted_scopes,
        requested_scopes=requested_scopes,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    view = ensure_usable(handle, now=now)
    gate = BindingCustodyGate(binding=binding, binding_fingerprint=view.binding_fingerprint)

    def _transport_for(descriptor: OperationDescriptor) -> Transport:
        return build_production_transport(
            gate=gate,
            store=binding_store,
            vault=vault,
            locator=drive_locator.locate,
            decode=drive_decode.for_operation(descriptor),
            http_send=http_send,
        )

    runner = _W01OperationRunner(
        handle=handle, layers=layers, transport_for=_transport_for, clock=clock
    )
    return DriveOperations(runner, page_size=page_size)


def make_drive_operations_factory(
    *,
    verifier: SubjectTenantVerifier,
    binding_store: BindingStore,
    vault: SecretStore,
    deployment_id: str,
    kiro_principal: str,
    clock: Callable[[], float],
    granted_scopes: Tuple[str, ...],
    layers: LayerCeilings,
    ttl_seconds: float,
    requested_scopes: Tuple[str, ...] = _DEFAULT_REQUESTED_SCOPES,
    page_size: int = 100,
    http_send: HttpSend = urllib_http_send,
) -> Callable[[Mapping[str, object]], DriveOperations]:
    """Return the ``operations_factory`` the connector expects: ``source -> DriveOperations``.

    This is what the host installs at
    ``app["knowledge_connector_runners"]["google_drive"]``. It captures the
    host-owned dependencies ONCE -- the verifier, the live binding store, the
    vault, the principal identifiers, the live ``clock``, and the AUTHORITY inputs
    (``granted_scopes`` from the real stored grant, the governance ``layers``, the
    handle ``ttl_seconds``) -- and reads the per-source subject / tenant from each
    ``source`` row when called. The connector calls this per sync;
    :func:`build_drive_operations` RESOLVES the trusted binding each time (never
    mints), so a source whose binding was revoked between syncs fails on the next
    sync instead of getting a fresh runner.
    """

    def _factory(source: Mapping[str, object]) -> DriveOperations:
        subject = str(source.get("subject") or "").strip()
        tenant = str(source.get("tenant") or "").strip()
        if not subject:
            raise ValueError(
                "Google Drive source requires a 'subject' (the verified identity "
                "the credential grant belongs to) to bind a runner"
            )
        if not tenant:
            raise ValueError("Google Drive source requires a 'tenant' to bind a runner")
        return build_drive_operations(
            subject=subject,
            tenant=tenant,
            verifier=verifier,
            binding_store=binding_store,
            vault=vault,
            deployment_id=deployment_id,
            kiro_principal=kiro_principal,
            granted_scopes=granted_scopes,
            layers=layers,
            clock=clock,
            ttl_seconds=ttl_seconds,
            requested_scopes=requested_scopes,
            page_size=page_size,
            http_send=http_send,
        )

    return _factory


__all__ = ["build_drive_operations", "make_drive_operations_factory"]
