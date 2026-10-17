"""Zoom connector contract semantics (W11-A).

Pure vendor logic and data for the Zoom connector: identity/UUID encoding,
pagination classification, AI Companion processing tri-state, and Zoom-shape
error mapping plus credential detection. It holds NO network, auth, or dispatch
code. The GENERIC error taxonomy, error envelope, redaction discipline, and
opaque pagination cursor live in the shared W01 control plane
(:mod:`kiro_crew.connections.control_plane`); this package folds into those and
keeps only Zoom-specific semantics. The owning spec is
``docs/system-specs/modules/connector-zoom.md``.
"""

from kiro_crew.connections.vendors.zoom.errors import (
    AMBIGUOUS_ABSENCE_CODE,
    classify_error,
    contains_zoom_credential,
    redact_zoom_secrets,
    zoom_operation_error,
)
from kiro_crew.connections.vendors.zoom.identity import (
    HOST_REACHABILITY_UNKNOWN,
    ZOOM_CREDENTIAL_MODES,
    ZOOM_SERVER_TO_SERVER,
    ZOOM_USER_OAUTH,
    OccurrenceTarget,
    OccurrenceTime,
    encode_uuid_path_segment,
    needs_double_encoding,
    occurrence_target,
    resolve_occurrence_time,
    series_id_for_instances,
)
from kiro_crew.connections.vendors.zoom.paging import (
    CursorRequest,
    Pagination,
    classify_pagination,
    next_cursor_request,
    to_next_cursor,
)
from kiro_crew.connections.vendors.zoom.processing import (
    SUMMARY_READY_WEBHOOK,
    SummaryState,
    classify_summary_state,
    is_summary_ready_signal,
)

__all__ = [
    "AMBIGUOUS_ABSENCE_CODE",
    "CursorRequest",
    "HOST_REACHABILITY_UNKNOWN",
    "OccurrenceTarget",
    "OccurrenceTime",
    "Pagination",
    "SUMMARY_READY_WEBHOOK",
    "SummaryState",
    "ZOOM_CREDENTIAL_MODES",
    "ZOOM_SERVER_TO_SERVER",
    "ZOOM_USER_OAUTH",
    "classify_error",
    "classify_pagination",
    "classify_summary_state",
    "contains_zoom_credential",
    "encode_uuid_path_segment",
    "is_summary_ready_signal",
    "needs_double_encoding",
    "next_cursor_request",
    "occurrence_target",
    "redact_zoom_secrets",
    "resolve_occurrence_time",
    "series_id_for_instances",
    "to_next_cursor",
    "zoom_operation_error",
]
