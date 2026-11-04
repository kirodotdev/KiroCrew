"""The ``kirocrew/status`` structured-status extension: its vocabulary and parser.

Moved whole out of :mod:`kiro_crew.acp.types`, which stays the import path and
re-exports every name defined here.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Structured status protocol (``kirocrew/status`` extension, version 1) ──
#
# ACP ``session/update`` carries tool-call start/complete and agent text;
# ``_kiro.dev/metadata`` carries the harness ``stopReason``; MCP
# ``notifications/progress`` is routed per request. None of them carries a WAIT
# REASON or a resume condition, so one versioned extension is added. It rides
# under ``params._meta[STATUS_EXTENSION_KEY]`` on a ``session/update`` frame and
# is TRUSTED ONLY FROM THE EXECUTION LAYER: the runtime accepts it from a frame
# routed to the session it names, never from a fanned-out frame and never from
# agent text (``AcpSessionHandle._handle_update`` enforces the origin rule; this
# module only owns the shape and the version gate). The MCP side — the same
# object under ``_meta.kirocrew_status`` on a ``notifications/progress``
# sibling — is documented in ``docs/system-specs/modules/acp-client.md`` and is
# NOT parsed here yet (the gateway stub is frozen for this PR).

STATUS_EXTENSION_KEY = "kirocrew/status"
STATUS_EXTENSION_VERSION = 1

STATUS_PHASE_STARTING = "starting"
STATUS_PHASE_RUNNING = "running"
STATUS_PHASE_WAITING = "waiting"
STATUS_PHASE_RECOVERING = "recovering"
STATUS_PHASES: frozenset[str] = frozenset(
    {STATUS_PHASE_STARTING, STATUS_PHASE_RUNNING, STATUS_PHASE_WAITING, STATUS_PHASE_RECOVERING}
)

#: Wait reasons — the taskq wait-state vocabulary (SPEC-ADDENDUM §1). A
#: ``waiting`` phase names exactly one of these; any other phase leaves it "".
WAIT_REASON_INPUT = "waiting_input"
WAIT_REASON_PERMISSION = "waiting_permission"
WAIT_REASON_DEPENDENCY = "waiting_dependency"
WAIT_REASON_CHILDREN = "waiting_children"
WAIT_REASON_RETRY = "retry_wait"
WAIT_REASONS: frozenset[str] = frozenset(
    {
        WAIT_REASON_INPUT,
        WAIT_REASON_PERMISSION,
        WAIT_REASON_DEPENDENCY,
        WAIT_REASON_CHILDREN,
        WAIT_REASON_RETRY,
    }
)

#: ``StructuredStatus.origin`` values. ``execution_layer`` is the only origin a
#: consumer may act on for a wait; ``liveness_oracle`` marks the watchdog's own
#: post-stall classification (evidence-based, no harness cooperation).
STATUS_ORIGIN_EXECUTION_LAYER = "execution_layer"
STATUS_ORIGIN_LIVENESS_ORACLE = "liveness_oracle"

#: ``StructuredStatus.progress_source`` values.
PROGRESS_SOURCE_TOOL_OUTPUT = "tool_output"
PROGRESS_SOURCE_STREAM_EVENT = "stream_event"
PROGRESS_SOURCE_CHECKPOINT = "checkpoint"
PROGRESS_SOURCE_PROCESS_EVIDENCE = "process_evidence"

_STATUS_STR_FIELDS = (
    "task_id",
    "session_id",
    "parent_id",
    "tool_call_id",
    "phase",
    "wait_reason",
    "dependency_scope",
    "progress_source",
    "checkpoint_ref",
)
_STATUS_BOOL_FIELDS = ("cancellable", "resumable", "safe_retry")
_STATUS_STR_MAX = 512


@dataclass(frozen=True)
class StructuredStatus:
    """One ``kirocrew/status`` (version 1) record.

    Identity: ``task_id`` / ``session_id`` / ``parent_id`` / ``tool_call_id`` /
    ``generation``. Lifecycle: ``phase`` (one of ``STATUS_PHASES``) and, when
    waiting, ``wait_reason`` (one of ``WAIT_REASONS``), ``dependency_scope`` and
    ``retry_at`` (epoch seconds, or None). Progress provenance:
    ``progress_source``. Capabilities the SCHEDULER may rely on:
    ``cancellable`` / ``resumable`` / ``safe_retry`` — each defaults to False so
    an emitter that says nothing grants nothing (a status that is uncertain
    must not invent a recoverable capability). ``checkpoint_ref`` names a
    checkpoint / partial result. ``origin`` is stamped by the consumer that
    validated the frame, never read from the wire.
    """

    version: int = STATUS_EXTENSION_VERSION
    task_id: str = ""
    session_id: str = ""
    parent_id: str = ""
    tool_call_id: str = ""
    generation: int = 0
    phase: str = STATUS_PHASE_RUNNING
    wait_reason: str = ""
    dependency_scope: str = ""
    retry_at: float | None = None
    progress_source: str = ""
    cancellable: bool = False
    resumable: bool = False
    safe_retry: bool = False
    checkpoint_ref: str = ""
    origin: str = STATUS_ORIGIN_EXECUTION_LAYER
    #: Free-text evidence for a ``liveness_oracle``-origin status (the oracle's
    #: evidence string). Empty for a wire-parsed status: the wire carries no
    #: prose field, on purpose.
    evidence: str = ""

    @property
    def is_waiting(self) -> bool:
        return self.phase == STATUS_PHASE_WAITING and bool(self.wait_reason)

    @classmethod
    def from_meta(cls, meta: object) -> "tuple[StructuredStatus | None, str]":
        """Parse ``params._meta`` into a status, or explain why not.

        Returns ``(status, "")`` on success and ``(None, reason)`` otherwise.
        ``reason`` is a closed set of short tokens: ``absent`` (no extension
        key — the ordinary case, never logged), ``not_object``,
        ``version_unsupported``, ``phase_invalid``, ``wait_reason_invalid``.
        Pure shape + version gate; it does NOT decide provenance (the caller
        knows which frame the meta came from — see the origin rule in
        ``AcpSessionHandle._handle_update``).

        Field handling is fail-closed: unknown fields are ignored, a string
        field that is not a string is dropped (""), a capability that is not a
        bool is False, ``generation`` that is not an int is 0, ``retry_at`` that
        is not a finite number is None. An unsupported version is rejected
        WHOLE — a consumer must not act on half of a newer schema.
        """
        if not isinstance(meta, dict):
            return None, "absent"
        raw = meta.get(STATUS_EXTENSION_KEY)
        if raw is None:
            return None, "absent"
        if not isinstance(raw, dict):
            return None, "not_object"
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            return None, "version_unsupported"
        if version != STATUS_EXTENSION_VERSION:
            return None, "version_unsupported"
        strs: dict[str, str] = {}
        for name in _STATUS_STR_FIELDS:
            value = raw.get(name)
            strs[name] = value[:_STATUS_STR_MAX] if isinstance(value, str) else ""
        phase = strs["phase"] or STATUS_PHASE_RUNNING
        if phase not in STATUS_PHASES:
            return None, "phase_invalid"
        wait_reason = strs["wait_reason"]
        if phase == STATUS_PHASE_WAITING:
            if wait_reason not in WAIT_REASONS:
                return None, "wait_reason_invalid"
        else:
            # A wait reason outside the waiting phase is noise, not a wait.
            wait_reason = ""
        generation_raw = raw.get("generation")
        generation = (
            generation_raw
            if isinstance(generation_raw, int) and not isinstance(generation_raw, bool)
            else 0
        )
        retry_raw = raw.get("retry_at")
        retry_at: float | None = None
        if isinstance(retry_raw, (int, float)) and not isinstance(retry_raw, bool):
            retry_f = float(retry_raw)
            if retry_f == retry_f and retry_f not in (float("inf"), float("-inf")):
                retry_at = retry_f
        bools = {name: raw.get(name) is True for name in _STATUS_BOOL_FIELDS}
        return (
            cls(
                version=version,
                task_id=strs["task_id"],
                session_id=strs["session_id"],
                parent_id=strs["parent_id"],
                tool_call_id=strs["tool_call_id"],
                generation=generation,
                phase=phase,
                wait_reason=wait_reason,
                dependency_scope=strs["dependency_scope"],
                retry_at=retry_at,
                progress_source=strs["progress_source"],
                cancellable=bools["cancellable"],
                resumable=bools["resumable"],
                safe_retry=bools["safe_retry"],
                checkpoint_ref=strs["checkpoint_ref"],
                origin=STATUS_ORIGIN_EXECUTION_LAYER,
            ),
            "",
        )
