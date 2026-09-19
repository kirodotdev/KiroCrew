"""Shared connector control plane (W01 · L01).

The one typed seam every provider stream (W02..W14) dispatches an operation
through: an :class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor`
(what the operation is), an
:class:`~kiro_crew.connections.control_plane.context.OperationContext` (the
references one call is made under -- never a credential value), an
:class:`~kiro_crew.connections.control_plane.result.OperationResult` (the
success/partial envelope carrying the SINGLE authoritative opaque pagination
cursor AND the
:data:`~kiro_crew.connections.control_plane.result.OperationPayload` holding the
data -- a collection of items, a single object, or raw bytes), and the
RUN-01
:class:`~kiro_crew.connections.control_plane.errors.OperationError` taxonomy.

W01 · L02 adds :class:`~kiro_crew.connections.control_plane.binding.Binding` --
AUTH-01's authorized-account record that a ``binding_ref`` points at: an
unguessable random id, a VERIFIED subject/tenant, a monotonic ``generation``
counter (for L04 revoke fencing; field + increment only here), and a
``secret_ref`` that carries the secret's location and metadata but never its
value.

L05 adds the per-operation permitted-credential-mode declaration and its
deny-by-default check
(:mod:`~kiro_crew.connections.control_plane.auth_modes`): an operation declares
which of the L01 credential modes it permits, and a caller-offered mode is
allowed or denied against that declaration (unstated == denied).

W01 · L08 adds :class:`~kiro_crew.connections.control_plane.handle.DerivedHandle`
-- a restricted, short-lived capability DERIVED from a trusted binding: its
scope set is a proven SUBSET of the binding's granted scopes, it carries an
absolute ``not_after`` TTL that :func:`~kiro_crew.connections.control_plane.handle.ensure_usable`
enforces, and it carries NOTHING that reconstructs the binding (no
``binding_id`` / subject / tenant / secret) -- only a one-way keyed
``binding_fingerprint`` and the ``generation``, both for L04 revoke fencing.

W01 · L09 adds the EXECUTOR
(:mod:`~kiro_crew.connections.control_plane.executor`) -- the caller that runs the
four judgments in one order before any transport call is emitted -- and its
PRODUCTION COMPOSITION
(:mod:`~kiro_crew.connections.control_plane.production`), which builds a real
transport out of the existing :class:`kiro_crew.secrets.SecretVault` custody and
a stdlib ``urllib.request`` client. Vendor request shaping is injected, not
implemented there.

Everything except :mod:`~kiro_crew.connections.control_plane.production` is pure
types and decisions with zero IO; that one module is where the seam actually
reaches a network, and it performs none at import time. This module is the control
plane's own export face, and it is the CANONICAL one: the wider
``kiro_crew.connections`` package does NOT re-export these symbols, so consumers
import them from ``kiro_crew.connections.control_plane`` (or its submodules),
never as ``kiro_crew.connections.<name>`` aliases.
"""

from kiro_crew.connections.control_plane.acl_binding_resolver import (
    AccessGrant,
    AccountToDeployment,
    ControlPlaneBindingResolver,
    StoreBackedAccountToDeployment,
    map_provider_to_service_id,
)
from kiro_crew.connections.control_plane.auth_modes import (
    AUTH_MODES_SCHEMA_VERSION,
    PermittedModeRegistry,
    PermittedModes,
    declare_permitted_modes,
    effective_permitted_modes,
    permit_operation,
    permit_registered_operation,
)
from kiro_crew.connections.control_plane.binding import (
    BINDING_SCHEMA_VERSION,
    INITIAL_GENERATION,
    SECRET_BACKEND_VAULT,
    Binding,
    BindingResolutionError,
    BindingVerificationError,
    SecretRef,
    SubjectTenantVerifier,
    VerifiedIdentity,
    binding_scoped_secret_ref,
    binding_secret_ref,
    create_binding,
    next_generation,
    resolve_binding_for_principal,
)
from kiro_crew.connections.control_plane.context import (
    CONTEXT_SCHEMA_VERSION,
    OperationContext,
)
from kiro_crew.connections.control_plane.errors import (
    ERROR_CLASSES,
    ERRORS_SCHEMA_VERSION,
    MAX_ERROR_CHARS,
    ErrorClass,
    OperationError,
    operation_error,
    redacted_detail,
)
from kiro_crew.connections.control_plane.executor import (
    EMPTY_RESPONSE_METADATA,
    EXECUTOR_SCHEMA_VERSION,
    Clock,
    ExecutionOutcome,
    PageWalk,
    PreconditionFailure,
    ResponseMetadata,
    Transport,
    TransportResponse,
    advance_page,
    classify_error,
    execute,
    is_non_idempotent_effect,
)
from kiro_crew.connections.control_plane.handle import (
    HANDLE_SCHEMA_VERSION,
    DerivedHandle,
    HandleExpiredError,
    HandleNotIssuedError,
    HandleScopeError,
    HandleTamperedError,
    TrustedHandleView,
    derive_handle,
    ensure_usable,
    is_expired,
)
from kiro_crew.connections.control_plane.lifecycle import (
    LIFECYCLE_SCHEMA_VERSION,
    BindingRevokedError,
    BindingStore,
    BindingStoreCorruptError,
    BindingUniquenessError,
    ResolvedCredential,
    SecretReader,
    StoredBinding,
    store_path,
)
from kiro_crew.connections.control_plane.operation import (
    CREDENTIAL_MODES,
    EFFECTS,
    OPERATION_KINDS,
    OPERATION_SCHEMA_VERSION,
    SERVICE_IDS,
    CredentialMode,
    Effect,
    OperationDescriptor,
    OperationKind,
    ServiceId,
)
from kiro_crew.connections.control_plane.policy import (
    LAYERS,
    POLICY_SCHEMA_VERSION,
    Approval,
    LayerCeilings,
    LayerName,
    approval_applies,
    decide,
    resolve_layers,
)
from kiro_crew.connections.control_plane.production import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    PRODUCTION_SCHEMA_VERSION,
    RESPONSE_METADATA_ALLOWLIST,
    BindingCustodyGate,
    BindingIdentityMismatchError,
    Decoded2xx,
    HttpReply,
    HttpRequest,
    HttpSend,
    MalformedResponseBodyError,
    RedirectHop,
    RedirectRefusedError,
    RequestLocator,
    ResponseTooLargeError,
    ResultDecode,
    SecretResolutionError,
    SecretStore,
    TransportDeadlineExceededError,
    build_production_transport,
    decode_json_body,
    neutral_decode,
    neutral_decode_detail,
    resolve_binding_secret,
    response_metadata,
    urllib_http_send,
)
from kiro_crew.connections.control_plane.result import (
    DEFAULT_MEDIA_TYPE,
    PAYLOAD_KIND_BYTES,
    PAYLOAD_KIND_COLLECTION,
    PAYLOAD_KIND_OBJECT,
    PAYLOAD_KINDS,
    RESULT_SCHEMA_VERSION,
    RESULT_STATUSES,
    BytesPayload,
    CollectionPayload,
    ObjectPayload,
    OperationPayload,
    OperationResult,
    PayloadKind,
    ResultStatus,
    result_with_payload,
)
from kiro_crew.connections.control_plane.writes import (
    ATTEMPT_OUTCOMES,
    REPLAY_VERDICTS,
    WRITES_SCHEMA_VERSION,
    AttemptOutcome,
    AttemptRecord,
    ReplayDecision,
    ReplayVerdict,
    args_fingerprint,
    record_attempt,
    replay_decision,
)

__all__ = [
    "ATTEMPT_OUTCOMES",
    "AUTH_MODES_SCHEMA_VERSION",
    "BINDING_SCHEMA_VERSION",
    "CONTEXT_SCHEMA_VERSION",
    "CREDENTIAL_MODES",
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_MEDIA_TYPE",
    "DEFAULT_TIMEOUT_SECONDS",
    "EFFECTS",
    "EMPTY_RESPONSE_METADATA",
    "ERRORS_SCHEMA_VERSION",
    "ERROR_CLASSES",
    "EXECUTOR_SCHEMA_VERSION",
    "HANDLE_SCHEMA_VERSION",
    "INITIAL_GENERATION",
    "LAYERS",
    "LIFECYCLE_SCHEMA_VERSION",
    "MAX_ERROR_CHARS",
    "OPERATION_KINDS",
    "OPERATION_SCHEMA_VERSION",
    "PAYLOAD_KINDS",
    "PAYLOAD_KIND_BYTES",
    "PAYLOAD_KIND_COLLECTION",
    "PAYLOAD_KIND_OBJECT",
    "POLICY_SCHEMA_VERSION",
    "PRODUCTION_SCHEMA_VERSION",
    "REPLAY_VERDICTS",
    "RESPONSE_METADATA_ALLOWLIST",
    "RESULT_SCHEMA_VERSION",
    "RESULT_STATUSES",
    "SECRET_BACKEND_VAULT",
    "SERVICE_IDS",
    "WRITES_SCHEMA_VERSION",
    "Approval",
    "AttemptOutcome",
    "AttemptRecord",
    "Binding",
    "BindingCustodyGate",
    "BindingIdentityMismatchError",
    "BindingResolutionError",
    "BindingRevokedError",
    "BindingStore",
    "BindingStoreCorruptError",
    "BindingUniquenessError",
    "BindingVerificationError",
    "BytesPayload",
    "Clock",
    "CollectionPayload",
    "CredentialMode",
    "Decoded2xx",
    "DerivedHandle",
    "Effect",
    "ErrorClass",
    "ExecutionOutcome",
    "HandleExpiredError",
    "HandleNotIssuedError",
    "HandleScopeError",
    "HandleTamperedError",
    "HttpReply",
    "HttpRequest",
    "HttpSend",
    "LayerCeilings",
    "LayerName",
    "MalformedResponseBodyError",
    "ObjectPayload",
    "OperationContext",
    "OperationDescriptor",
    "OperationError",
    "OperationKind",
    "OperationPayload",
    "OperationResult",
    "PageWalk",
    "PayloadKind",
    "PermittedModeRegistry",
    "PermittedModes",
    "PreconditionFailure",
    "RedirectHop",
    "RedirectRefusedError",
    "ReplayDecision",
    "ReplayVerdict",
    "RequestLocator",
    "ResolvedCredential",
    "ResponseTooLargeError",
    "ResponseMetadata",
    "ResultDecode",
    "ResultStatus",
    "SecretReader",
    "SecretRef",
    "SecretResolutionError",
    "SecretStore",
    "ServiceId",
    "StoredBinding",
    "SubjectTenantVerifier",
    "Transport",
    "TransportDeadlineExceededError",
    "TransportResponse",
    "TrustedHandleView",
    "VerifiedIdentity",
    "advance_page",
    "approval_applies",
    "args_fingerprint",
    "binding_secret_ref",
    "binding_scoped_secret_ref",
    "resolve_binding_for_principal",
    "AccessGrant",
    "AccountToDeployment",
    "ControlPlaneBindingResolver",
    "StoreBackedAccountToDeployment",
    "map_provider_to_service_id",
    "build_production_transport",
    "classify_error",
    "create_binding",
    "decide",
    "declare_permitted_modes",
    "decode_json_body",
    "derive_handle",
    "effective_permitted_modes",
    "ensure_usable",
    "execute",
    "is_expired",
    "is_non_idempotent_effect",
    "neutral_decode",
    "neutral_decode_detail",
    "next_generation",
    "operation_error",
    "permit_operation",
    "permit_registered_operation",
    "record_attempt",
    "redacted_detail",
    "replay_decision",
    "resolve_binding_secret",
    "resolve_layers",
    "response_metadata",
    "result_with_payload",
    "store_path",
    "urllib_http_send",
]
