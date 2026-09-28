"""Shared connector control plane (W01 · L01).

The one typed seam every provider stream (W02..W14) dispatches an operation
through: an :class:`~kiro_crew.connections.control_plane.operation.OperationDescriptor`
(what the operation is), an
:class:`~kiro_crew.connections.control_plane.context.OperationContext` (the
references one call is made under -- never a credential value), an
:class:`~kiro_crew.connections.control_plane.result.OperationResult` (the
success/partial envelope with an opaque pagination cursor), and the RUN-01
:class:`~kiro_crew.connections.control_plane.errors.OperationError` taxonomy.

Pure types, zero IO. This module is the control plane's own export face, and it
is the CANONICAL one: the wider ``kiro_crew.connections`` package does NOT
re-export these symbols, so consumers import them from
``kiro_crew.connections.control_plane`` (or its submodules), never as
``kiro_crew.connections.<name>`` aliases.
"""

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
from kiro_crew.connections.control_plane.result import (
    RESULT_SCHEMA_VERSION,
    RESULT_STATUSES,
    OperationResult,
    ResultStatus,
)

__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "CREDENTIAL_MODES",
    "EFFECTS",
    "ERRORS_SCHEMA_VERSION",
    "ERROR_CLASSES",
    "MAX_ERROR_CHARS",
    "OPERATION_KINDS",
    "OPERATION_SCHEMA_VERSION",
    "RESULT_SCHEMA_VERSION",
    "RESULT_STATUSES",
    "SERVICE_IDS",
    "CredentialMode",
    "Effect",
    "ErrorClass",
    "OperationContext",
    "OperationDescriptor",
    "OperationError",
    "OperationKind",
    "OperationResult",
    "ResultStatus",
    "ServiceId",
    "operation_error",
    "redacted_detail",
]
