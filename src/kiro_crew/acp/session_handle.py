"""AcpSessionHandle — one multiplexed ACP session on a shared runtime.

Split out of ``runtime.py`` to keep the two responsibilities in separate files:

- ``session_handle.py`` (this file): the per-session API surface — one
  ``sessionId`` + its ``asyncio.Queue``, the prompt/cancel/approve/reject event
  loop, and the per-session stale/stall watchdog. Depends only on the runtime
  *protocol* (``AcpRuntimeProtocol``), the shared dispatch parser, and the ACP
  types — never on the concrete ``AcpRuntime``. The class statement lives in
  ``session_handle_class.py`` and binds each member from the ``session_handle_*``
  owner whose responsibility it is; this module stays its import path.
- ``runtime.py``: ``AcpRuntime`` — owns the subprocess + single-reader demux and
  constructs handles.

The runtime exceptions (``AcpRuntimeError`` / ``AcpRuntimeDead``) and the
``AcpRuntimeProtocol`` live in ``session_handle_runtime_contract.py`` (the lower
layer) and are re-exported here, so ``runtime.py`` imports them from this module
without a circular import; ``runtime.py`` re-exports them so
existing ``from kiro_crew.acp.runtime import AcpSessionHandle`` call sites keep
working.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew import _facade_mirror, acp_tool_gate, model_registry, permission_floor
from kiro_crew.acp import kas_wire
from kiro_crew.acp._dispatch import (
    DRAIN_YIELD_AFTER_S,
    MAX_ACTIVE_TOOL_CALLS,
    TOOL_CALL_CACHE_MAX_ENTRIES,
    TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
    build_permission_event,
    classify_notification,
    error_is_refusal_terminal,
    identified_mcp_call,
    is_mcp_tool_approval,
    is_unattributed_error_candidate,
    kas_crew_agent_id,
    liveness_key,
    parse_codex_compaction_update,
    parse_metadata,
    parse_prompt_token_usage,
    parse_refusal,
    parse_session_update,
    parse_text_chunk,
    parse_usage_cost,
    parse_usage_update,
    prompt_is_adapter_command,
    redact_text,
    reject_option_id,
    release_tool_call_provenance,
    scope_overflowed,
    scoped_tool_cache_key,
    set_mode_params,
    set_model_params,
    unattributed_terminal_error,
)
from kiro_crew.acp.client import (
    _COMPACTION_FAILED_TURN_BUDGET,
    DEFAULT_MODEL,
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    AcpProcessDied,
    AcpTimeoutError,
    AcpToolGateUnroutable,
    _effective_prompt_timeout_async,
    _is_config_value_rejection,
    _is_safe_oauth_url,
    _is_tool_interrupted_marker,
    _jsonrpc_error_code,
    _loggable_request_id,
    _push_model_via_effort_split,
    _raise_acp_error,
    advertised_model_ids,
    catalog_row_would_drop,
    compaction_failure_detail,
    compaction_failure_is_transient,
    format_command_result,
    parse_slash_command,
    pick_served_default,
    prompt_timeout_for_ceiling,
    registration_rate_limited_error,
    registration_throttle_line,
    resolve_usable_model,
)
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_REMOTE_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    INTERACTIVE_NARROWING_RISKS,
    INTERACTIVE_NONE,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    InFlightToolTracker,
    InteractiveClassification,
    LivenessOracle,
    ToolCallState,
    _consume_future_exception,
    boottime_now,
    classify_interactive_command,
    consult_offloaded,
    steady_now,
)
from kiro_crew.acp.mcp_reattempt import MAX_ATTEMPTS as REATTEMPT_MAX
from kiro_crew.acp.mcp_reattempt import is_recoverable_auth_failure
from kiro_crew.acp.mcp_session_report import (
    BUCKET_CAP,
    NAME_CAP,
    KasMcpReadiness,
    McpSessionReport,
    sanitize_sink_text,
)
from kiro_crew.acp.prompt_blocks import build_prompt_blocks, summarize_prompt_structure
from kiro_crew.acp.session_handle_class import AcpSessionHandle
from kiro_crew.acp.session_handle_compaction import _POST_COMPACTION_METADATA_GRACE_SECS
from kiro_crew.acp.session_handle_kas import _KAS_HOOKS_METHODS
from kiro_crew.acp.session_handle_lifecycle import _CANCEL_GRACE_SECS
from kiro_crew.acp.session_handle_mcp import (
    _LAUNCH_MAX_ARGS,
    _LAUNCH_MAX_TOKEN,
    _LAUNCHES_PER_SERVER,
    _MCP_DRAIN_NO_REPORT_CEILING,
    _SENTINEL,
    mcp_launch_roster,
)
from kiro_crew.acp.session_handle_model_selection import (
    _READ_PATH_PROBE_DEADLINE_SECS,
    _READ_PATH_REPROBE_MIN_INTERVAL_SECS,
    _SET_MODEL_REPLY_TIMEOUT,
    advertised_models_from_session,
    model_select_envelope,
    models_from_config_options,
    parse_advertised_models,
    session_models_envelope,
)
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_LABEL_CAP,
    NATIVE_CHILD_NOT_RESUMABLE,
    NATIVE_CHILD_ROSTER_CAP,
    NATIVE_CHILDREN_UNCHARGED_KIND,
    _bounded_label,
)
from kiro_crew.acp.session_handle_runtime_contract import (
    AcpAgentUnavailable,
    AcpFrameTooLarge,
    AcpModeNotFound,
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
    AcpRuntimeProtocol,
)
from kiro_crew.acp.session_handle_steering import _STEERING_SETTLE_SECS
from kiro_crew.acp.session_handle_watchdog import (
    _TURN_BOUNDED_WINDOWS,
    _TURN_CEILING_WINDOW_FRACTION,
    _WORKING_LOG_INTERVAL_SECS,
    _WORKING_NEVER_LOGGED,
    _WORKING_WARN_AFTER_SECS,
    _WORKING_WARN_DEADLINE_FRACTION,
    INTERACTIVE_POLICY_CANCEL,
    INTERACTIVE_POLICY_WAIT,
    WatchdogSettings,
    _load_watchdog_settings,
    _watchdog_evidence_class,
)
from kiro_crew.acp.skill_reads import SkillReadTracker
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION,
    ACP_BACKENDS_HOOKS_LIST,
    ACP_BACKENDS_INLINE_COMPACTION,
    ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
    ACP_BACKENDS_STRUCTURED_REFUSAL,
    ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_STEER_CLEARED,
    EVENT_STEER_CONSUMED,
    EVENT_STEER_QUEUED,
    EVENT_STRUCTURED_STATUS,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TODO_UPDATE,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    JSONRPC_METHOD_NOT_FOUND,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
    METHOD_CANCEL,
    METHOD_COMMANDS_EXECUTE,
    METHOD_KAS_CHECKPOINT_REVERT,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_SESSION_COMPACT,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_STEERING,
    METHOD_SET_CONFIG_OPTION,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    MODEL_CONFIG_ID,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    PROGRESS_SOURCE_PROCESS_EVIDENCE,
    STATUS_EXTENSION_KEY,
    STATUS_ORIGIN_LIVENESS_ORACLE,
    STATUS_PHASE_WAITING,
    STEERING_ANSWER_WAIT_SECS,
    STEERING_INJECTED,
    STEERING_STARTED_NEW_TURN,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_CONTENT_FILTERED_WIRE,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    TERMINAL_TOOL_STATUSES,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CURRENT_MODE,
    UPDATE_SESSION_INFO,
    WAIT_REASON_INPUT,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    StructuredStatus,
    effort_config_option_id,
    steering_outcome,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.drivers.acp import EntitlementRevalidating  # noqa: F401 - re-exported
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS
from kiro_crew.executors import subprocess_executor
from kiro_crew.metrics.events import CHILD_PERMISSION_DENIED, emit_counter
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.recovery.ladder import InfraError, classify_infra_error
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.trust_patterns import GrantDeadline, GrantEnded, grant_deadline_guard
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Compatibility facade. ``AcpSessionHandle``'s moved members live in the owner modules
# below, one responsibility each, bound by name in the class body, and the module-level
# code moved with the members that read it. Every name this module bound before the
# owners existed stays readable as ``kiro_crew.acp.session_handle.<name>``:
#
# * Every import of this module stays bound here, so a patch of
#   ``kiro_crew.acp.session_handle.<name>`` reaches every owner that binds the same
#   object (``_facade_mirror``), as it reached this module's own code.
# * A moved name that callers or this module's own code read is an ordinary import above.
# * Every other moved name is SERVED: absent from this namespace on purpose, read from
#   its owner through the mirror, and a write or delete through this module lands on
#   every owner binding it.
#
# ``test/test_session_handle_bindings.py`` pins the member inventory, the binding and the
# reach.
# --------------------------------------------------------------------------- #
#: Owner modules, lowest layer first: an owner imports only owners listed before it.
_SESSION_HANDLE_OWNERS: tuple[str, ...] = (
    "kiro_crew.acp.session_handle_runtime_contract",
    "kiro_crew.acp.session_handle_watchdog",
    "kiro_crew.acp.session_handle_mcp",
    "kiro_crew.acp.session_handle_kas",
    "kiro_crew.acp.session_handle_native_children",
    "kiro_crew.acp.session_handle_model_selection",
    "kiro_crew.acp.session_handle_steering",
    "kiro_crew.acp.session_handle_lifecycle",
    "kiro_crew.acp.session_handle_compaction",
    "kiro_crew.acp.session_handle_permissions",
    "kiro_crew.acp.session_handle_elicitation",
    "kiro_crew.acp.session_handle_updates",
    "kiro_crew.acp.session_handle_turn",
    "kiro_crew.acp.session_handle_dispatch",
    "kiro_crew.acp.session_handle_class",
)
for _owner in _SESSION_HANDLE_OWNERS:
    _importlib.import_module(_owner)
#: Module-level names this module bound before the split that only owner code reads.
_SERVED: frozenset[str] = frozenset(
    {
        "_COMMAND_TURN_TIMEOUT_SECS",
        "_DRAIN_CLOSED_STOP_REASONS",
        "_MAX_INFLIGHT_HOOK_EXECUTIONS",
        "_MCP_DRAIN_DURATION",
        "_MCP_DRAIN_IDLE_EXIT",
        "_MCP_DRAIN_REPORT_ACTIONS",
        "_PROMPT_TRANSPORT_COMMANDS",
        "_READ_PATH_SPAWN_RACE_SECS",
        "_clamp_to_prompt_ceiling",
        "_launch_argv",
        "_select_options",
        "_warn_if_above_chat_ceiling",
    }
)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _SESSION_HANDLE_OWNERS,
    excluded=(),
    surface=frozenset(globals()) | _SERVED,
)
_EXPORTS.update(_MIRROR.served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.acp.session_handle_kas import (
        _MAX_INFLIGHT_HOOK_EXECUTIONS,
    )
    from kiro_crew.acp.session_handle_mcp import (
        _MCP_DRAIN_DURATION,
        _MCP_DRAIN_IDLE_EXIT,
        _MCP_DRAIN_REPORT_ACTIONS,
        _launch_argv,
    )
    from kiro_crew.acp.session_handle_model_selection import (
        _READ_PATH_SPAWN_RACE_SECS,
        _select_options,
    )
    from kiro_crew.acp.session_handle_turn import (
        _COMMAND_TURN_TIMEOUT_SECS,
        _DRAIN_CLOSED_STOP_REASONS,
        _PROMPT_TRANSPORT_COMMANDS,
    )
    from kiro_crew.acp.session_handle_watchdog import (
        _clamp_to_prompt_ceiling,
        _warn_if_above_chat_ceiling,
    )
